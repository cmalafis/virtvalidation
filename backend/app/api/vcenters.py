"""vCenter source registry + scale-aware RVTools upload helpers.

Two related surfaces share this router:

  - CRUD on :class:`VCenterSource` rows so operators can register the
    vCenters that VMs belong to.
  - The RVTools **delta preview** endpoint — given a parsed RVTools
    payload from the frontend, returns ``{new, updated, removed,
    unchanged}`` against the existing inventory in the vCenter scope
    so the operator confirms before committing.

Commit itself rides on the existing :func:`/api/vms/bulk` endpoint —
the frontend submits the new + updated VMs there with
``source_vcenter_id`` set on each row. Keeping the commit path on the
existing surface means audit / dedup / status semantics stay shared.
"""

from __future__ import annotations

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.categorizer import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    run_categorization_task,
)
from app.core.categorizer import task_store as categorization_task_store
from app.core.db import get_db
from app.core.rvtools_import import (
    ASYNC_IMPORT_THRESHOLD,
    run_rvtools_import,
    run_rvtools_import_async,
)
from app.core.rvtools_import import task_store as rvtools_import_task_store
from app.models.grouping import GroupKind, VMGroup, VMGroupMember
from app.models.vcenter import VCenterSource
from app.models.vm import VM
from app.schemas.vcenter import (
    CategorizationGroupRead,
    CategorizationTaskRead,
    RVToolsDeltaItem,
    RVToolsDeltaRequest,
    RVToolsDeltaResponse,
    RVToolsImportTaskRead,
    VCenterSourceCreate,
    VCenterSourceRead,
    VCenterSourceUpdate,
)

router = APIRouter(tags=["vcenters"])


def _vm_count(db: Session, source_id: int) -> int:
    """COUNT subquery — keep separate so the bulk listing endpoint can
    batch all counts in a single query instead of N+1."""
    return (
        db.scalar(
            select(func.count(VM.id)).where(VM.source_vcenter_id == source_id)
        )
        or 0
    )


def _read_payload(db: Session, row: VCenterSource) -> dict:
    body = VCenterSourceRead.model_validate(row).model_dump(mode="json")
    body["vm_count"] = _vm_count(db, row.id)
    return body


def _get_or_404(db: Session, vcenter_id: int) -> VCenterSource:
    row = db.get(VCenterSource, vcenter_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"vCenter source {vcenter_id} not found"
        )
    return row


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
@router.get("", response_model=list[VCenterSourceRead])
def list_vcenters(db: Session = Depends(get_db)) -> list[dict]:
    """Return every registered vCenter with its current VM count.

    A single GROUP BY query handles the count so we don't N+1 across
    the listing — important once a customer registers a few dozen
    vCenters.
    """
    rows = list(db.scalars(select(VCenterSource).order_by(VCenterSource.name)).all())
    counts = dict(
        db.execute(
            select(VM.source_vcenter_id, func.count(VM.id)).group_by(
                VM.source_vcenter_id
            )
        ).all()
    )
    out: list[dict] = []
    for row in rows:
        body = VCenterSourceRead.model_validate(row).model_dump(mode="json")
        body["vm_count"] = int(counts.get(row.id, 0))
        out.append(body)
    return out


@router.post("", response_model=VCenterSourceRead, status_code=status.HTTP_201_CREATED)
def create_vcenter(
    request: Request,
    payload: VCenterSourceCreate,
    db: Session = Depends(get_db),
) -> dict:
    row = VCenterSource(**payload.model_dump())
    db.add(row)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"vCenter source named {payload.name!r} already exists",
        ) from e
    db.refresh(row)
    record_audit(
        db,
        action="vcenter.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="vcenter",
        resource_id=row.id,
        details={
            "name": row.name,
            "hostname": row.hostname,
            "classification_level": row.classification_level.value,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return _read_payload(db, row)


@router.get("/{vcenter_id}", response_model=VCenterSourceRead)
def get_vcenter(vcenter_id: int, db: Session = Depends(get_db)) -> dict:
    return _read_payload(db, _get_or_404(db, vcenter_id))


@router.patch("/{vcenter_id}", response_model=VCenterSourceRead)
def update_vcenter(
    request: Request,
    vcenter_id: int,
    payload: VCenterSourceUpdate,
    db: Session = Depends(get_db),
) -> dict:
    row = _get_or_404(db, vcenter_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        request.state.skip_audit_log = True
        return _read_payload(db, row)

    for field, value in updates.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    record_audit(
        db,
        action="vcenter.update",
        actor=request.headers.get("x-actor", "user"),
        resource_type="vcenter",
        resource_id=row.id,
        details={"name": row.name, "fields": sorted(updates.keys())},
    )
    db.commit()
    request.state.skip_audit_log = True
    return _read_payload(db, row)


@router.delete("/{vcenter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vcenter(
    request: Request,
    vcenter_id: int,
    db: Session = Depends(get_db),
) -> None:
    """Delete a vCenter source. Member VMs keep existing — their
    ``source_vcenter_id`` flips to NULL. Operators who want a full
    purge follow up with a bulk VM delete; that's a deliberate
    two-step to avoid silent fleet wipes.

    We explicitly NULL the FK in a single UPDATE rather than relying
    on the schema's ``ON DELETE SET NULL`` clause — the clause is
    correct, but SQLite (used in tests) only honors FK actions when
    ``PRAGMA foreign_keys=ON`` and we'd rather not rely on that being
    set everywhere. Explicit UPDATE works the same on every backend.
    """
    row = _get_or_404(db, vcenter_id)
    affected = _vm_count(db, vcenter_id)
    record_audit(
        db,
        action="vcenter.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="vcenter",
        resource_id=row.id,
        details={
            "name": row.name,
            "hostname": row.hostname,
            "vms_unassigned": affected,
        },
    )
    if affected:
        db.query(VM).filter(VM.source_vcenter_id == vcenter_id).update(
            {VM.source_vcenter_id: None}, synchronize_session=False
        )
    db.delete(row)
    db.commit()
    request.state.skip_audit_log = True


# ---------------------------------------------------------------------------
# vCenter hostname auto-match
# ---------------------------------------------------------------------------
def _normalize_hostname(raw: str | None) -> str:
    """Lowercase + strip trailing dots. Mirrors the JS normalizer so
    both sides cluster `vc.corp.` and `VC.CORP` to the same key."""
    if not raw:
        return ""
    return str(raw).strip().lower().rstrip(".")


@router.post("/auto-match")
def auto_match_vcenters(
    payload: dict, db: Session = Depends(get_db)
) -> dict:
    """Resolve a list of RVTools-detected vCenter hostnames to registered
    VCenterSource rows.

    Matching strategy:

      1. **Exact** — normalized hostname equals row.hostname (normalized).
      2. **Fuzzy** — the detected hostname is a prefix of a registered
         hostname (or vice versa). Covers ``vc-east-01`` vs
         ``vc-east-01.dha.mil`` mismatches the operator routinely
         creates by trimming the FQDN on registration.

    Returns:
        ``{matches: [{hostname, normalized, matched_vcenter_id,
                       matched_vcenter_name, confidence}],
           unmatched: [...]}``
    """
    hostnames = payload.get("hostnames") or []
    if not isinstance(hostnames, list):
        raise HTTPException(
            status_code=422, detail="`hostnames` must be a list"
        )

    rows = list(db.scalars(select(VCenterSource)).all())
    by_norm: dict[str, VCenterSource] = {
        _normalize_hostname(r.hostname): r for r in rows if r.hostname
    }

    matches: list[dict] = []
    unmatched: list[str] = []

    for raw in hostnames:
        norm = _normalize_hostname(raw)
        if not norm:
            continue
        # Exact normalized match.
        exact = by_norm.get(norm)
        if exact is not None:
            matches.append(
                {
                    "hostname": raw,
                    "normalized": norm,
                    "matched_vcenter_id": exact.id,
                    "matched_vcenter_name": exact.name,
                    "confidence": "exact",
                }
            )
            continue
        # Fuzzy: prefix-equivalent. We accept either direction so
        # short vs FQDN-registered both resolve.
        fuzzy_match: VCenterSource | None = None
        for reg_norm, row in by_norm.items():
            if not reg_norm:
                continue
            if norm.startswith(reg_norm + ".") or reg_norm.startswith(norm + "."):
                fuzzy_match = row
                break
        if fuzzy_match is not None:
            matches.append(
                {
                    "hostname": raw,
                    "normalized": norm,
                    "matched_vcenter_id": fuzzy_match.id,
                    "matched_vcenter_name": fuzzy_match.name,
                    "confidence": "fuzzy",
                }
            )
        else:
            unmatched.append(raw)

    return {"matches": matches, "unmatched": unmatched}


# ---------------------------------------------------------------------------
# RVTools delta preview
# ---------------------------------------------------------------------------
@router.post(
    "/{vcenter_id}/rvtools/preview",
    response_model=RVToolsDeltaResponse,
)
def rvtools_delta_preview(
    vcenter_id: int,
    payload: RVToolsDeltaRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Compare an uploaded RVTools VM list against current inventory.

    Match key: VM name within the vCenter scope. RVTools' own UUID
    column would be more reliable but most customer exports lose it
    after spreadsheet edits, so name-within-scope is the durable key.

    Returns four buckets the UI renders so the operator confirms what
    they're about to commit:

      - **new**       — name not in the vCenter's existing VMs
      - **updated**   — name matches but at least one tracked field
                        changed (with a per-field diff)
      - **removed**   — VM exists in the vCenter scope but not in the
                        upload (operator decides whether to mark
                        decommissioned or skip)
      - **unchanged** — exact match, no diff
    """
    _get_or_404(db, vcenter_id)

    existing = list(
        db.scalars(select(VM).where(VM.source_vcenter_id == vcenter_id)).all()
    )
    by_name = {vm.name: vm for vm in existing}
    incoming_names: set[str] = set()

    new: list[dict] = []
    updated: list[dict] = []
    unchanged: list[dict] = []
    seen_in_payload: set[str] = set()

    for entry in payload.vms:
        if entry.name in seen_in_payload:
            # Within-payload duplicates collapse silently here so the
            # operator sees one row per logical VM. Real audit lives in
            # the bulk-create endpoint that follows.
            continue
        seen_in_payload.add(entry.name)
        incoming_names.add(entry.name)
        existing_vm = by_name.get(entry.name)
        if existing_vm is None:
            new.append({"name": entry.name})
            continue
        diff = _diff_vm_row(existing_vm, entry)
        if diff:
            updated.append({"name": entry.name, "diff": diff})
        else:
            unchanged.append({"name": entry.name})

    removed = [
        RVToolsDeltaItem(name=name).model_dump()
        for name in sorted(set(by_name) - incoming_names)
    ]

    return {
        "new": [RVToolsDeltaItem(**n).model_dump() for n in new],
        "updated": [RVToolsDeltaItem(**u).model_dump() for u in updated],
        "removed": removed,
        "unchanged": [RVToolsDeltaItem(**u).model_dump() for u in unchanged],
        "summary": {
            "new": len(new),
            "updated": len(updated),
            "removed": len(removed),
            "unchanged": len(unchanged),
            "total_in_payload": len(seen_in_payload),
            "total_in_vcenter": len(existing),
        },
    }


# Fields the delta engine treats as material — changes here mean
# "updated", changes anywhere else are ignored. Kept narrow so casual
# edits to ``notes`` don't cascade as updated rows.
_TRACKED_FIELDS = (
    "source_hostname",
    "ip_address",
    "os_family",
    "role",
    "environment",
    "owner",
    "application_hint",
    "vsphere_networks",
    "vsphere_datastores",
)


def _diff_vm_row(existing: VM, incoming) -> dict:
    """Return per-field {before, after} for fields that actually changed.

    Absent / None / empty fields in the incoming payload are treated as
    "operator didn't supply this column in their RVTools export, keep
    the existing value" rather than "clear to None". RVTools exports
    routinely omit the IP / role / owner columns; flagging those as
    "updated" would flood the operator's review queue with noise.
    """
    diff: dict[str, dict] = {}
    for field in _TRACKED_FIELDS:
        after = getattr(incoming, field, None)
        if field.startswith("vsphere_"):
            # Compare lists order-insensitively — RVTools exports often
            # reshuffle the network order between captures. An empty
            # incoming list means "RVTools didn't have this column" and
            # is treated as no-change against any existing value.
            if not after:
                continue
            before_norm = sorted(getattr(existing, field, None) or [])
            after_norm = sorted(after or [])
            if before_norm != after_norm:
                diff[field] = {"before": before_norm, "after": after_norm}
        else:
            # Missing scalar = keep current value, not "clear to None".
            if after is None or after == "":
                continue
            before = getattr(existing, field, None)
            if (before or "") != after:
                diff[field] = {"before": before, "after": after}
    return diff


# ---------------------------------------------------------------------------
# RVTools import (commit)
# ---------------------------------------------------------------------------
@router.post("/{vcenter_id}/rvtools/import")
def rvtools_import(
    request: Request,
    vcenter_id: int,
    payload: RVToolsDeltaRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Persist an RVTools delta against a vCenter scope.

    Same payload shape as ``/preview``. Behavior:

      - Under :data:`ASYNC_IMPORT_THRESHOLD` VMs (default 500): run
        synchronously, return the import counts inline.
      - At or above the threshold: spawn a BackgroundTask, return 202
        with a ``task_id`` the operator polls.

    "Removed" rows aren't auto-deleted — they're flagged
    ``missing_from_last_upload=True`` and the operator decides whether
    to decommission. This is a deliberate two-step (the same shape
    as :func:`delete_vcenter`) to avoid silent fleet wipes.
    """
    _get_or_404(db, vcenter_id)
    actor = request.headers.get("x-actor", "user")

    record_audit(
        db,
        action="vcenter.rvtools_import_triggered",
        actor=actor,
        resource_type="vcenter",
        resource_id=vcenter_id,
        details={"vm_count": len(payload.vms)},
    )
    db.commit()
    request.state.skip_audit_log = True

    if len(payload.vms) >= ASYNC_IMPORT_THRESHOLD:
        task = rvtools_import_task_store.create(vcenter_id=vcenter_id)
        # The Pydantic models aren't pickleable through the worker
        # boundary in every backend, but BackgroundTasks runs in-process
        # so we hand the validated list straight through.
        background_tasks.add_task(
            run_rvtools_import_async,
            task.task_id,
            vcenter_id=vcenter_id,
            payload_vms=list(payload.vms),
            actor=actor,
        )
        # FastAPI returns 200 by default for response_model unions; we
        # surface the async branch via the body's shape (presence of
        # task_id + status). Operators detect async by the absence of
        # `created` etc. on the response.
        return task.to_dict()

    summary = run_rvtools_import(
        db,
        vcenter_id=vcenter_id,
        payload_vms=payload.vms,
        actor=actor,
    )
    return {
        "created": summary.created,
        "updated": summary.updated,
        "marked_missing": summary.marked_missing,
        "unchanged": summary.unchanged,
        "errors": summary.errors,
    }


@router.get(
    "/{vcenter_id}/rvtools/import/{task_id}",
    response_model=RVToolsImportTaskRead,
)
def get_rvtools_import_status(vcenter_id: int, task_id: str) -> dict:
    task = rvtools_import_task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"RVTools import task {task_id} not found. Tasks live in "
                "memory only and may have been cleared by an appliance "
                "restart — re-trigger if needed."
            ),
        )
    if task.vcenter_id != vcenter_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Task {task_id} belongs to vcenter {task.vcenter_id}, "
                f"not {vcenter_id}"
            ),
        )
    return task.to_dict()


# ---------------------------------------------------------------------------
# Level 1 categorization
# ---------------------------------------------------------------------------
@router.post(
    "/{vcenter_id}/categorize",
    response_model=CategorizationTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_categorization(
    request: Request,
    vcenter_id: int,
    background_tasks: BackgroundTasks,
    batch_size: int = Query(default=DEFAULT_BATCH_SIZE, ge=1, le=MAX_BATCH_SIZE),
    db: Session = Depends(get_db),
) -> dict:
    """Spawn a Level 1 categorization run as a BackgroundTask.

    Returns 202 immediately with the task handle; the LLM batches run
    after the response is sent so the dashboard can poll for progress.
    Re-running on the same vCenter replaces existing groups — the
    categorizer is idempotent on the same scope.
    """
    vcenter = _get_or_404(db, vcenter_id)
    actor = request.headers.get("x-actor", "user")

    task = categorization_task_store.create(source_vcenter_id=vcenter_id)
    record_audit(
        db,
        action="categorization.triggered",
        actor=actor,
        resource_type="vcenter",
        resource_id=vcenter.id,
        details={
            "vcenter_name": vcenter.name,
            "task_id": task.task_id,
            "batch_size": batch_size,
        },
    )
    db.commit()

    background_tasks.add_task(
        run_categorization_task,
        task.task_id,
        vcenter_id,
        batch_size=batch_size,
    )
    request.state.skip_audit_log = True
    return task.to_dict()


@router.get(
    "/{vcenter_id}/categorize/{task_id}",
    response_model=CategorizationTaskRead,
)
def get_categorization_status(vcenter_id: int, task_id: str) -> dict:
    task = categorization_task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Categorization task {task_id} not found. Tasks are kept in "
                "memory only and may have been cleared by an appliance restart "
                "— re-trigger if needed."
            ),
        )
    if task.source_vcenter_id != vcenter_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Task {task_id} belongs to vcenter {task.source_vcenter_id}, "
                f"not {vcenter_id}"
            ),
        )
    return task.to_dict()


@router.get(
    "/{vcenter_id}/groups",
    response_model=list[CategorizationGroupRead],
)
def list_groups(
    vcenter_id: int,
    kind: GroupKind | None = Query(default=None),
    db: Session = Depends(get_db),
) -> list[dict]:
    """List the groups Level 1 categorization produced for a vCenter.

    Optional ``kind`` filter restricts to one axis (application /
    environment / business_unit / other) — the dashboard renders the
    three axes side-by-side and pulls each one separately.
    """
    _get_or_404(db, vcenter_id)

    stmt = (
        select(VMGroup)
        .where(VMGroup.source_vcenter_id == vcenter_id)
        .order_by(VMGroup.kind, VMGroup.name)
    )
    if kind is not None:
        stmt = stmt.where(VMGroup.kind == kind)

    rows = list(db.scalars(stmt).all())
    if not rows:
        return []

    # Single GROUP BY query for the member counts so the listing
    # endpoint stays O(1) regardless of how many groups exist.
    counts = dict(
        db.execute(
            select(VMGroupMember.group_id, func.count(VMGroupMember.id))
            .where(
                VMGroupMember.group_id.in_([r.id for r in rows])
            )
            .group_by(VMGroupMember.group_id)
        ).all()
    )

    return [
        {
            "id": row.id,
            "kind": row.kind.value,
            "name": row.name,
            "description": row.description,
            "vm_count": int(counts.get(row.id, 0)),
        }
        for row in rows
    ]
