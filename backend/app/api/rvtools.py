"""Top-level RVTools upload endpoint that routes per-VM into the
correct vCenter source based on the operator-supplied mapping.

The per-vCenter endpoints in ``app.api.vcenters`` stay for back-compat;
this surface is the one the new auto-link upload flow uses.

Request shape:

```json
{
  "vms": [ ... parsed RVTools rows, each with optional source_vcenter_hostname ... ],
  "vcenter_mapping": {
    "vc-east-01.corp.local": 1,
    "vc-east-02.corp.local": 2
  },
  "create_missing": false
}
```

Response:

```json
{
  "imported_per_vcenter": {"1": {"created": 200, ...}, ...},
  "errors": [...],
  "skipped": [...],
  "elapsed_seconds": 12.4
}
```

Single transaction across all vCenters — either the whole upload
commits or none of it does, with one exception: ``errors`` collects
per-VM failures (bad row shape) but doesn't roll back the bulk run.
That's the "partial success" mode the spec asks for.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import get_db
from app.core.limits import MAX_VMS_PER_RVTOOLS_IMPORT
from app.core.rvtools_import import run_rvtools_import
from app.models.vcenter import VCenterSource
from app.schemas.vcenter import RVToolsVMRow

logger = logging.getLogger(__name__)

router = APIRouter(tags=["rvtools"])


class MultiVCenterImportRequest(BaseModel):
    """Payload for the auto-link upload flow.

    ``vcenter_mapping`` maps each RVTools-detected hostname (the
    ``source_vcenter_hostname`` the parser stamped on each VM) to a
    registered :class:`VCenterSource.id`. Hostnames not present in
    the mapping cause their VMs to be skipped with a clear error.
    """

    # Capacity cap mirrors the RVTools-import core; both default to
    # 10K via app.core.limits. Operators with larger fleets bump
    # MAX_VMS_PER_RVTOOLS_IMPORT in the env.
    vms: list[RVToolsVMRow] = Field(min_length=1, max_length=MAX_VMS_PER_RVTOOLS_IMPORT)
    # hostname → vcenter_id
    vcenter_mapping: dict[str, int] = Field(default_factory=dict)
    # When True, VMs whose hostname isn't in the mapping fall back
    # to the default_vcenter_id (operator's "everything-else" bucket).
    default_vcenter_id: int | None = None


class PerVCenterResult(BaseModel):
    vcenter_id: int
    vcenter_name: str
    created: int
    updated: int
    marked_missing: int
    unchanged: int


class MultiVCenterImportResult(BaseModel):
    imported_per_vcenter: list[PerVCenterResult]
    skipped: list[dict]
    errors: list[str]
    total_vms: int
    elapsed_seconds: float


@router.post(
    "/upload-multi-vcenter",
    response_model=MultiVCenterImportResult,
)
def upload_multi_vcenter(
    request: Request,
    payload: MultiVCenterImportRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Auto-link RVTools VMs to vCenter sources based on the parser's
    detected hostname.

    The pipeline:

      1. Group VMs by their ``source_vcenter_hostname``.
      2. Resolve each hostname to a vCenter id via the mapping (or
         the ``default_vcenter_id`` fallback).
      3. Per vCenter, run the existing :func:`run_rvtools_import`
         delta-import core. Returns a per-vCenter breakdown.
      4. Any VM whose hostname can't be resolved lands in ``skipped``
         with a specific reason. Partial-success is the default
         contract: a missing mapping does NOT fail the whole upload.
    """
    actor = request.headers.get("x-actor", "user")
    started = time.monotonic()

    # Normalize the mapping keys so the parser's "vc-east-01.corp.local"
    # and the operator's "VC-East-01.CORP.LOCAL." cluster equivalently.
    normalized_mapping: dict[str, int] = {}
    for raw_host, vc_id in payload.vcenter_mapping.items():
        key = _normalize_hostname(raw_host)
        if not key:
            continue
        normalized_mapping[key] = vc_id

    # Pre-fetch the vCenter rows so we can validate ids + emit names
    # in the result without N round-trips inside the loop.
    referenced_ids = set(normalized_mapping.values())
    if payload.default_vcenter_id is not None:
        referenced_ids.add(payload.default_vcenter_id)
    vcenter_rows: dict[int, VCenterSource] = {}
    if referenced_ids:
        for row in db.scalars(
            select(VCenterSource).where(VCenterSource.id.in_(referenced_ids))
        ).all():
            vcenter_rows[row.id] = row
    missing_ids = referenced_ids - set(vcenter_rows)
    if missing_ids:
        raise HTTPException(
            status_code=409,
            detail=(
                f"vcenter_mapping references unknown vcenter id(s): "
                f"{sorted(missing_ids)}. Create them first or remove from mapping."
            ),
        )

    # Group VMs by their detected hostname.
    by_vcenter_id: dict[int, list[RVToolsVMRow]] = {}
    skipped: list[dict] = []
    for vm in payload.vms:
        host_key = _normalize_hostname(vm.source_vcenter_hostname)
        target_id: Optional[int] = normalized_mapping.get(host_key) if host_key else None
        if target_id is None:
            target_id = payload.default_vcenter_id
        if target_id is None:
            # Operator didn't map this hostname and didn't supply a
            # default — skip with a specific reason so the UI can
            # surface which hostname is unmapped.
            skipped.append(
                {
                    "vm_name": vm.name,
                    "detected_hostname": vm.source_vcenter_hostname,
                    "reason": (
                        "no vcenter mapping for this RVTools hostname; "
                        "set it in vcenter_mapping or supply default_vcenter_id"
                    ),
                }
            )
            continue
        by_vcenter_id.setdefault(target_id, []).append(vm)

    if not by_vcenter_id:
        raise HTTPException(
            status_code=422,
            detail="No VM could be routed to a vCenter source",
        )

    per_results: list[dict] = []
    errors: list[str] = []
    total = 0

    for vc_id, vm_subset in by_vcenter_id.items():
        vc_row = vcenter_rows[vc_id]
        try:
            summary = run_rvtools_import(
                db,
                vcenter_id=vc_id,
                payload_vms=vm_subset,
                actor=actor,
            )
        except Exception as e:  # noqa: BLE001 — surface per-vcenter, keep going
            logger.exception("multi-vcenter import failed for vc_id=%s: %s", vc_id, e)
            errors.append(f"vc_id={vc_id}: {e}")
            continue
        per_results.append(
            {
                "vcenter_id": vc_id,
                "vcenter_name": vc_row.name,
                "created": summary.created,
                "updated": summary.updated,
                "marked_missing": summary.marked_missing,
                "unchanged": summary.unchanged,
            }
        )
        total += summary.created + summary.updated + summary.unchanged

    record_audit(
        db,
        action="rvtools.multi_vcenter_imported",
        actor=actor,
        resource_type="rvtools_upload",
        resource_id=None,
        details={
            "vm_count": len(payload.vms),
            "vcenters_touched": len(per_results),
            "skipped": len(skipped),
            "errors": len(errors),
            "elapsed_seconds": round(time.monotonic() - started, 2),
        },
    )
    db.commit()
    request.state.skip_audit_log = True

    return {
        "imported_per_vcenter": per_results,
        "skipped": skipped,
        "errors": errors,
        "total_vms": len(payload.vms),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _normalize_hostname(raw: str | None) -> str:
    if not raw:
        return ""
    return str(raw).strip().lower().rstrip(".")
