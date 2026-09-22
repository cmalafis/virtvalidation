"""RVTools delta-import core for the JSON endpoints (pre-parsed rows).

The UI no longer uses this path: file uploads go through
``app.core.import_jobs`` (server-side streaming parse, batched commits,
MoRef identity, multi-sheet hardware facts). This module stays for API
clients that POST already-parsed rows to ``/api/rvtools/upload-multi-vcenter``
or ``/api/sources/vcenters/{id}/rvtools/import``; it matches by name only
and captures none of the hardware facts.

Splits the work the import endpoint does into a pure function that can
run synchronously (small uploads) or inside a BackgroundTask (large
uploads, where the operator polls a task store for status).

The import is **idempotent** by design: re-running with the same
payload produces zero `created` / `updated` / `marked_missing` rows
on the second pass, just `unchanged`. Operators can re-upload the
same RVTools export safely.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.audit import record_audit
from app.models.vm import VM, VMStatus

logger = logging.getLogger(__name__)


# Threshold above which the API endpoint returns 202 + a task handle
# instead of running the import inside the request. Tuned so that an
# operator's bulk RVTools export (under 500 VMs) completes in-line —
# the request stays under the FastAPI worker's typical 60s budget —
# while larger fleets get the async path.
ASYNC_IMPORT_THRESHOLD = 500


# Tracked fields the import treats as "material" — same set the delta
# preview uses, kept in sync so a row marked "updated" in preview
# actually applies updates here.
IMPORT_TRACKED_FIELDS = (
    "source_hostname",
    "ip_address",
    "os_family",
    "role",
    "environment",
    "owner",
    "application_hint",
    "vsphere_networks",
    "vsphere_datastores",
    "vsphere_cluster",
    "vsphere_folder",
)


@dataclass
class RVToolsImportSummary:
    created: int = 0
    updated: int = 0
    marked_missing: int = 0
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def _apply_incoming_value(vm: VM, field_name: str, incoming) -> bool:
    """Apply one tracked field from an incoming RVTools row.

    Mirrors the preview's logic: missing scalar = keep current,
    empty list = keep current. Returns True iff the model row
    actually changed.
    """
    if field_name.startswith("vsphere_"):
        if not incoming:
            return False
        before = sorted(getattr(vm, field_name) or [])
        after = sorted(incoming)
        if before == after:
            return False
        setattr(vm, field_name, list(after))
        return True
    # Scalar
    if incoming is None or incoming == "":
        return False
    before = getattr(vm, field_name)
    if (before or "") == incoming:
        return False
    setattr(vm, field_name, incoming)
    return True


def _detect_environment(vm: VM, *, explicit: bool) -> None:
    from app.core.environment import Environment, detect_environment

    if explicit:
        vm.environment_source = "user_set"
        return
    if vm.environment or vm.environment_source == "user_set":
        return
    found = detect_environment(
        name=vm.name,
        folder_path=vm.vsphere_folder,
        cluster=vm.vsphere_cluster,
        custom_attributes=vm.custom_attributes,
    )
    if found.environment != Environment.UNKNOWN:
        vm.environment = found.environment.value
        vm.environment_source = "auto_detected"


def run_rvtools_import(
    db: Session,
    *,
    vcenter_id: int,
    payload_vms: Iterable,
    actor: str,
) -> RVToolsImportSummary:
    """Apply an RVTools delta against a vCenter scope.

    ``payload_vms`` is the validated ``RVToolsDeltaRequest.vms`` list —
    each entry is a Pydantic model with the per-row fields plus
    ``model_dump()`` available.

    Behavior per bucket:

      - **new**: create a VM with ``source_vcenter_id`` set,
        ``status='discovered'``, ``last_seen_in_upload_at=now``.
      - **updated**: apply tracked-field changes, stamp
        ``last_seen_in_upload_at``, clear ``missing_from_last_upload``.
      - **unchanged**: stamp ``last_seen_in_upload_at`` only.
      - **missing**: VM in scope that's absent from the upload —
        flip ``missing_from_last_upload=True``. Never auto-deletes.

    One audit row per material change (created / updated / marked
    missing). Unchanged VMs don't generate audit noise.
    """
    summary = RVToolsImportSummary()
    now = datetime.now(timezone.utc)

    existing = list(db.scalars(select(VM).where(VM.source_vcenter_id == vcenter_id)).all())
    by_name = {vm.name: vm for vm in existing}
    seen_payload_names: set[str] = set()

    for entry in payload_vms:
        name = getattr(entry, "name", None)
        if not name:
            summary.errors.append("payload row missing name")
            continue
        if name in seen_payload_names:
            # Within-payload duplicates collapse silently — preview
            # surfaces them; here we trust the operator confirmed.
            continue
        seen_payload_names.add(name)

        existing_vm = by_name.get(name)
        if existing_vm is None:
            data = entry.model_dump()
            vm = VM(
                name=name,
                source_hostname=data.get("source_hostname") or name,
                ip_address=data.get("ip_address"),
                os_family=data.get("os_family"),
                role=data.get("role"),
                environment=data.get("environment"),
                owner=data.get("owner"),
                application_hint=data.get("application_hint"),
                vsphere_networks=list(data.get("vsphere_networks") or []),
                vsphere_datastores=list(data.get("vsphere_datastores") or []),
                vsphere_cluster=data.get("vsphere_cluster"),
                vsphere_folder=data.get("vsphere_folder"),
                custom_attributes=dict(data.get("custom_attributes") or {}),
                source_vcenter_id=vcenter_id,
                status=VMStatus.discovered,
                missing_from_last_upload=False,
                last_seen_in_upload_at=now,
            )
            _detect_environment(vm, explicit=bool(data.get("environment")))
            db.add(vm)
            summary.created += 1
            db.flush()
            record_audit(
                db,
                action="vm.rvtools_import.create",
                actor=actor,
                resource_type="vm",
                resource_id=vm.id,
                details={
                    "name": vm.name,
                    "source_vcenter_id": vcenter_id,
                },
            )
            continue

        changed_fields: dict[str, dict] = {}
        for fname in IMPORT_TRACKED_FIELDS:
            before = getattr(existing_vm, fname)
            if _apply_incoming_value(existing_vm, fname, getattr(entry, fname, None)):
                after = getattr(existing_vm, fname)
                changed_fields[fname] = {"before": before, "after": after}

        # Always stamp last-seen and clear the missing flag — even when
        # no tracked field changed, the VM is still present in this
        # upload, so a previously-missing flag should clear.
        was_missing = existing_vm.missing_from_last_upload
        existing_vm.missing_from_last_upload = False
        existing_vm.last_seen_in_upload_at = now

        if changed_fields:
            summary.updated += 1
            record_audit(
                db,
                action="vm.rvtools_import.update",
                actor=actor,
                resource_type="vm",
                resource_id=existing_vm.id,
                details={"name": existing_vm.name, "changes": changed_fields},
            )
        elif was_missing:
            # No field changes, but it had been marked missing — log
            # the "reappeared" event so operators see the lifecycle.
            record_audit(
                db,
                action="vm.rvtools_import.reappeared",
                actor=actor,
                resource_type="vm",
                resource_id=existing_vm.id,
                details={"name": existing_vm.name},
            )
            summary.unchanged += 1
        else:
            summary.unchanged += 1

    # Anything in scope that wasn't in the payload is now "missing".
    for name, vm in by_name.items():
        if name in seen_payload_names:
            continue
        if not vm.missing_from_last_upload:
            vm.missing_from_last_upload = True
            summary.marked_missing += 1
            record_audit(
                db,
                action="vm.rvtools_import.marked_missing",
                actor=actor,
                resource_type="vm",
                resource_id=vm.id,
                details={"name": vm.name, "source_vcenter_id": vcenter_id},
            )
        # else: already missing from a prior upload — idempotent

    db.commit()
    logger.info(
        "rvtools import vcenter=%s actor=%s created=%d updated=%d "
        "marked_missing=%d unchanged=%d",
        vcenter_id,
        actor,
        summary.created,
        summary.updated,
        summary.marked_missing,
        summary.unchanged,
    )
    return summary


# ---------------------------------------------------------------------------
# Background-task store for large imports
# ---------------------------------------------------------------------------
ImportStatus = Literal["running", "completed", "failed"]


@dataclass
class RVToolsImportTask:
    task_id: str
    vcenter_id: int
    status: ImportStatus = "running"
    progress_percent: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    result: RVToolsImportSummary | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("started_at", "completed_at"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        if isinstance(self.result, RVToolsImportSummary):
            d["result"] = asdict(self.result)
        return d


class RVToolsImportTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, RVToolsImportTask] = {}
        self._lock = threading.RLock()

    def create(self, vcenter_id: int) -> RVToolsImportTask:
        task = RVToolsImportTask(task_id=str(uuid.uuid4()), vcenter_id=vcenter_id)
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> RVToolsImportTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def mark_completed(self, task_id: str, result: RVToolsImportSummary) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "completed"
            t.progress_percent = 100
            t.result = result
            t.completed_at = datetime.now(timezone.utc)

    def mark_failed(self, task_id: str, error: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "failed"
            t.error = error
            t.completed_at = datetime.now(timezone.utc)


task_store = RVToolsImportTaskStore()


def run_rvtools_import_async(
    task_id: str,
    *,
    vcenter_id: int,
    payload_vms: list,
    actor: str,
) -> None:
    """BackgroundTask body. Opens its own session because the request
    that spawned it has already returned by the time this fires."""
    db = _db_module.SessionLocal()
    try:
        result = run_rvtools_import(
            db,
            vcenter_id=vcenter_id,
            payload_vms=payload_vms,
            actor=actor,
        )
        task_store.mark_completed(task_id, result)
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("rvtools async import task %s failed", task_id)
        task_store.mark_failed(task_id, str(e))
    finally:
        db.close()
