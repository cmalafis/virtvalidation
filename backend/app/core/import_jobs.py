"""Background runner for server-side inventory imports.

Two passes over the spooled upload, both streaming (see
``app.core.rvtools_parser``):

  1. **scan** — count VM rows per detected vCenter hostname so the
     operator can route each hostname to a registered source *before*
     anything is written.
  2. **import** — upsert VMs in batches of ``MAX_VMS_PER_CHUNK``, one
     transaction per batch, updating the ``ImportJob`` row as it goes.

Idempotency: a VM is matched within its vCenter by MoRef when the export
carries one, otherwise by name. Re-importing the same workbook yields
``created=0, updated=0``. ``mode="create_only"`` never touches a matched
row; ``mode="upsert"`` (default) applies changed fields and, at the end,
flags VMs of the touched vCenters that were absent from the file as
``missing_from_last_upload`` (never deletes).

Operator decisions survive re-import: an ``environment`` whose
``environment_source`` is ``user_set`` is never overwritten, and
lifecycle / target overrides are not import-tracked fields at all.
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.assessment import assess, facts_from_vm
from app.core.audit import record_audit
from app.core.environment import Environment, detect_environment
from app.core.limits import MAX_VMS_PER_CHUNK, MAX_VMS_PER_RVTOOLS_IMPORT
from app.core.rvtools_parser import (
    ParsedVM,
    ParseStats,
    RowIssue,
    RVToolsParseError,
    iter_vms,
    normalize_hostname,
    scan,
)
from app.models.import_job import IMPORT_ACTIVE_STATUSES, ImportJob, ImportJobReject
from app.models.vcenter import VCenterSource
from app.models.vm import VM, VMStatus

logger = logging.getLogger(__name__)

# Fields never applied on update: identity is the match key, and
# ``name`` changes are handled explicitly (rename-by-MoRef).
_NOT_TRACKED = frozenset({"name"})
# Logged by key only — the before/after of a disks list is noise in an
# audit row, and the full document is on the VM anyway.
_AUDIT_KEYS_ONLY = frozenset({"hardware_facts", "custom_attributes", "notes"})

_ORPHAN_MESSAGE = (
    "Import interrupted by appliance restart; batches committed before the "
    "restart are kept. Upload the file again to finish — re-import is idempotent."
)


class _Cancelled(Exception):
    pass


def spool_dir() -> Path:
    d = Path(os.environ.get("IMPORT_SPOOL_DIR") or Path(tempfile.gettempdir()) / "vv-imports")
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cleanup_file(job: ImportJob) -> None:
    if job.file_path:
        try:
            Path(job.file_path).unlink(missing_ok=True)
        except OSError:  # pragma: no cover — best effort
            logger.warning("import %s: could not remove spool file", job.id)
        job.file_path = None


def _finish(db: Session, job: ImportJob, status: str, error: str | None = None) -> None:
    job.status = status
    job.error_message = error[:2048] if error else None
    job.completed_at = _now()
    job.current_sheet = None
    _cleanup_file(job)
    db.commit()


# --------------------------------------------------------------------------
# Pass 1 — scan
# --------------------------------------------------------------------------
def run_scan(job_id: str) -> None:
    """BackgroundTask body. Opens its own session (the request is gone)."""
    db = _db_module.SessionLocal()
    try:
        job = db.get(ImportJob, job_id)
        if job is None or job.status != "uploaded":
            return
        job.status = "scanning"
        job.started_at = _now()
        job.progress_message = "Reading the VM sheet"
        db.commit()

        try:
            result = scan(Path(job.file_path))
        except RVToolsParseError as e:
            _finish(db, job, "failed", str(e))
            return

        if result.vm_rows > MAX_VMS_PER_RVTOOLS_IMPORT:
            _finish(
                db,
                job,
                "failed",
                f"The file has {result.vm_rows} VM rows; this appliance accepts up to "
                f"{MAX_VMS_PER_RVTOOLS_IMPORT} per import (MAX_VMS_PER_RVTOOLS_IMPORT). "
                "Split the export by vCenter or raise the limit.",
            )
            return

        registered = {
            normalize_hostname(v.hostname): v.id
            for v in db.scalars(select(VCenterSource)).all()
            if v.hostname
        }
        job.detected_vcenters = [
            {"hostname": host, "vm_count": count, "suggested_vcenter_id": registered.get(host)}
            for host, count in sorted(result.detected_vcenters.items(), key=lambda kv: -kv[1])
        ]
        job.rows_total = result.vm_rows
        job.result = {"sheets_found": result.sheets_found}

        if job.vcenter_mapping or job.default_vcenter_id is not None:
            db.commit()
            _run_import(db, job)
        else:
            job.status = "awaiting_mapping"
            job.progress_message = "Waiting for vCenter routing"
            db.commit()
    except Exception as e:  # noqa: BLE001 — a job must always reach a terminal state
        logger.exception("import %s scan crashed", job_id)
        db.rollback()
        job = db.get(ImportJob, job_id)
        if job is not None:
            _finish(db, job, "failed", f"Unexpected error while scanning: {e}")
    finally:
        db.close()


# --------------------------------------------------------------------------
# Pass 2 — import
# --------------------------------------------------------------------------
def run_import(job_id: str) -> None:
    db = _db_module.SessionLocal()
    try:
        job = db.get(ImportJob, job_id)
        if job is None or job.status != "importing":
            return
        _run_import(db, job)
    finally:
        db.close()


class _VCenterIndex:
    """Existing VMs of one vCenter, keyed both ways, loaded once."""

    def __init__(self, db: Session, vcenter_id: int):
        self.by_moref: dict[str, VM] = {}
        self.by_name: dict[str, VM] = {}
        # Names aren't unique within a vCenter, so ``by_name`` can shadow
        # rows; the missing-sweep walks ``all`` instead.
        self.all: list[VM] = []
        self.seen_ids: set[int] = set()
        for vm in db.scalars(select(VM).where(VM.source_vcenter_id == vcenter_id)).all():
            self.add(vm)

    def add(self, vm: VM) -> None:
        self.all.append(vm)
        if vm.moref:
            self.by_moref[vm.moref] = vm
        self.by_name[vm.name] = vm

    def match(self, fields: dict[str, Any]) -> VM | None:
        moref = fields.get("moref")
        if moref and moref in self.by_moref:
            return self.by_moref[moref]
        hit = self.by_name.get(fields["name"])
        # A name match whose MoRef disagrees is a different VM that reused
        # the name — don't merge them.
        if hit is not None and moref and hit.moref and hit.moref != moref:
            return None
        return hit


def _apply_environment(vm: VM) -> None:
    if vm.environment_source == "user_set" or vm.environment:
        return
    detected = detect_environment(
        name=vm.name,
        folder_path=vm.vsphere_folder,
        cluster=vm.vsphere_cluster,
        custom_attributes=vm.custom_attributes,
    )
    if detected.environment != Environment.UNKNOWN:
        vm.environment = detected.environment.value
        vm.environment_source = "auto_detected"


def _apply_assessment(vm: VM) -> None:
    result = assess(facts_from_vm(vm)).to_dict()
    if vm.assessment != result:  # don't dirty the row (and updated_at) for nothing
        vm.assessment = result
        vm.assessment_status = result["status"]
        vm.assessment_finding_ids = [f["id"] for f in result["findings"]]


def _apply_fields(vm: VM, fields: dict[str, Any]) -> dict[str, Any]:
    """Apply incoming values; missing/empty incoming keeps the current
    value (a thinner re-export must not blank good data). Returns the
    audit-shaped change set."""
    changes: dict[str, Any] = {}
    for key, incoming in fields.items():
        if key in _NOT_TRACKED or incoming in (None, "", [], {}):
            continue
        before = getattr(vm, key)
        if before == incoming:
            continue
        setattr(vm, key, incoming)
        changes[key] = (
            "changed" if key in _AUDIT_KEYS_ONLY else {"before": before, "after": incoming}
        )
    if fields["name"] != vm.name:  # matched by MoRef → renamed in vSphere
        changes["name"] = {"before": vm.name, "after": fields["name"]}
        vm.name = fields["name"]
    return changes


class _Importer:
    def __init__(self, db: Session, job: ImportJob):
        self.db = db
        self.job = job
        self.now = _now()
        self.mapping = {
            normalize_hostname(k) or "": v for k, v in (job.vcenter_mapping or {}).items()
        }
        self.indexes: dict[int, _VCenterIndex] = {}
        self.per_vcenter: dict[int, Counter] = {}
        self.unrouted: Counter = Counter()
        self.first_row_for: dict[tuple[int, str], int] = {}

    # -- routing ---------------------------------------------------------
    def route(self, item: ParsedVM) -> int | None:
        target = self.mapping.get(item.vcenter_hostname or "")
        return target if target is not None else self.job.default_vcenter_id

    def index(self, vcenter_id: int) -> _VCenterIndex:
        if vcenter_id not in self.indexes:
            self.indexes[vcenter_id] = _VCenterIndex(self.db, vcenter_id)
        return self.indexes[vcenter_id]

    # -- one VM ----------------------------------------------------------
    def upsert(self, vcenter_id: int, item: ParsedVM, delta: Counter, created: list[VM]) -> None:
        idx = self.index(vcenter_id)
        vm = idx.match(item.fields)
        if vm is None:
            vm = VM(
                **item.fields,
                source_vcenter_id=vcenter_id,
                status=VMStatus.discovered,
                missing_from_last_upload=False,
                last_seen_in_upload_at=self.now,
            )
            _apply_environment(vm)
            _apply_assessment(vm)
            self.db.add(vm)
            idx.add(vm)
            created.append(vm)
            delta[(vcenter_id, "created")] += 1
            return

        if self.job.mode == "create_only":
            delta[(vcenter_id, "unchanged")] += 1
            if vm.id is not None:
                idx.seen_ids.add(vm.id)
            return

        old_name = vm.name
        changes = _apply_fields(vm, item.fields)
        if "name" in changes:
            idx.by_name.pop(old_name, None)
            idx.by_name[vm.name] = vm
        _apply_environment(vm)
        _apply_assessment(vm)
        reappeared = vm.missing_from_last_upload
        vm.missing_from_last_upload = False
        vm.last_seen_in_upload_at = self.now
        if vm.id is not None:
            idx.seen_ids.add(vm.id)
        if changes:
            delta[(vcenter_id, "updated")] += 1
            record_audit(
                self.db,
                action="vm.rvtools_import.update",
                actor=self.job.actor,
                resource_type="vm",
                resource_id=vm.id,
                details={"name": vm.name, "import_job": self.job.id, "changes": changes},
            )
        else:
            delta[(vcenter_id, "unchanged")] += 1
            if reappeared:
                record_audit(
                    self.db,
                    action="vm.rvtools_import.reappeared",
                    actor=self.job.actor,
                    resource_type="vm",
                    resource_id=vm.id,
                    details={"name": vm.name, "import_job": self.job.id},
                )

    # -- one batch = one transaction --------------------------------------
    def flush_batch(self, batch: list[tuple[int, ParsedVM]], issues: list[RowIssue]) -> None:
        if not batch and not issues:
            return
        try:
            self._commit(batch, issues)
        except IntegrityError:
            # One bad row must not sink 499 good ones. Replay the batch a
            # row at a time; the offender lands in the reject report.
            self.db.rollback()
            self.indexes.clear()  # cached rows may hold rolled-back state
            self.job = self.db.get(ImportJob, self.job.id)
            for entry in batch:
                try:
                    self._commit([entry], [])
                except IntegrityError as e:
                    self.db.rollback()
                    self.indexes.clear()
                    self.job = self.db.get(ImportJob, self.job.id)
                    self._commit(
                        [],
                        [
                            RowIssue(
                                self.job.current_sheet or "vInfo",
                                entry[1].row_number,
                                entry[1].fields["name"],
                                "conflicts with an existing VM in this vCenter "
                                f"(duplicate name or MoRef): {type(e.orig).__name__}",
                            )
                        ],
                    )
            self._commit([], issues)

    def _commit(self, batch: list[tuple[int, ParsedVM]], issues: list[RowIssue]) -> None:
        delta: Counter = Counter()
        created: list[VM] = []
        for vcenter_id, item in batch:
            self.upsert(vcenter_id, item, delta, created)
        for issue in issues:
            self.db.add(
                ImportJobReject(
                    job_id=self.job.id,
                    sheet=issue.sheet[:64],
                    row_number=issue.row_number,
                    vm_name=(issue.vm_name or None) and issue.vm_name[:255],
                    severity=issue.severity,
                    reason=issue.reason[:512],
                )
            )
        self.db.flush()
        if created:
            by_vc: dict[int, list[int]] = {}
            for vm in created:
                by_vc.setdefault(vm.source_vcenter_id, []).append(vm.id)
                self.index(vm.source_vcenter_id).seen_ids.add(vm.id)
            for vcenter_id, ids in by_vc.items():
                # One audit row per batch per vCenter, not per VM: 5,000
                # "created" rows would bury the audit log. The ids are here.
                record_audit(
                    self.db,
                    action="vm.rvtools_import.create_batch",
                    actor=self.job.actor,
                    resource_type="import_job",
                    resource_id=self.job.id,
                    details={"source_vcenter_id": vcenter_id, "count": len(ids), "vm_ids": ids},
                )
        job = self.job
        job.rows_valid += len(batch)
        job.rows_rejected += sum(1 for i in issues if i.severity == "rejected")
        job.rows_warned += sum(1 for i in issues if i.severity == "warning")
        for (vcenter_id, kind), n in delta.items():
            self.per_vcenter.setdefault(vcenter_id, Counter())[kind] += n
            setattr(job, f"{kind}_count", getattr(job, f"{kind}_count") + n)
        self.db.commit()

    # -- missing sweep -----------------------------------------------------
    def mark_missing(self) -> None:
        for vcenter_id, idx in self.indexes.items():
            ids: list[int] = []
            for vm in idx.all:
                if vm.id in idx.seen_ids or vm.missing_from_last_upload:
                    continue
                vm.missing_from_last_upload = True
                ids.append(vm.id)
            if ids:
                self.per_vcenter.setdefault(vcenter_id, Counter())["marked_missing"] += len(ids)
                self.job.marked_missing_count += len(ids)
                record_audit(
                    self.db,
                    action="vm.rvtools_import.marked_missing_batch",
                    actor=self.job.actor,
                    resource_type="import_job",
                    resource_id=self.job.id,
                    details={"source_vcenter_id": vcenter_id, "count": len(ids), "vm_ids": ids},
                )
        self.db.commit()


def _run_import(db: Session, job: ImportJob) -> None:
    job.status = "importing"
    job.started_at = job.started_at or _now()
    job.progress_message = "Importing"
    # Counters restart so a retried job can't double count.
    job.rows_read = job.rows_valid = job.rows_rejected = job.rows_warned = 0
    job.created_count = job.updated_count = job.unchanged_count = job.marked_missing_count = 0
    db.commit()

    imp = _Importer(db, job)
    stats = ParseStats()

    def on_progress(s: ParseStats) -> None:
        j = imp.job
        j.rows_read = s.rows_read
        j.rows_total = s.rows_total
        j.current_sheet = s.current_sheet
        db.commit()
        db.refresh(j, attribute_names=["cancel_requested"])
        if j.cancel_requested:
            raise _Cancelled

    try:
        batch: list[tuple[int, ParsedVM]] = []
        issues: list[RowIssue] = []
        for item in iter_vms(Path(job.file_path), stats, on_progress):
            if isinstance(item, RowIssue):
                issues.append(item)
                continue
            vcenter_id = imp.route(item)
            if vcenter_id is None:
                imp.unrouted[item.vcenter_hostname or ""] += 1
                continue
            key = (vcenter_id, item.fields.get("moref") or f"name:{item.fields['name']}")
            first = imp.first_row_for.setdefault(key, item.row_number)
            if first != item.row_number:
                issues.append(
                    RowIssue(
                        stats.current_sheet or "vInfo",
                        item.row_number,
                        item.fields["name"],
                        f"duplicate of row {first} in this file — first occurrence kept",
                    )
                )
                continue
            batch.append((vcenter_id, item))
            if len(batch) >= MAX_VMS_PER_CHUNK:
                imp.flush_batch(batch, issues)
                batch, issues = [], []
                on_progress(stats)
        imp.flush_batch(batch, issues)

        job = imp.job
        if job.mode == "upsert":
            imp.mark_missing()

        names = {v.id: v.name for v in db.scalars(select(VCenterSource)).all()}
        touched = list(imp.per_vcenter) or [-1]
        env_rows = db.execute(
            select(VM.environment, func.count())
            .where(VM.source_vcenter_id.in_(touched))
            .group_by(VM.environment)
        ).all()
        job.result = {
            **(job.result or {}),
            "sheets_found": stats.sheets_found,
            "per_vcenter": [
                {
                    "vcenter_id": vc,
                    "vcenter_name": names.get(vc, f"vcenter {vc}"),
                    "created": c["created"],
                    "updated": c["updated"],
                    "unchanged": c["unchanged"],
                    "marked_missing": c["marked_missing"],
                }
                for vc, c in sorted(imp.per_vcenter.items())
            ],
            "skipped_unrouted": [
                {"hostname": h, "vm_count": n} for h, n in imp.unrouted.most_common()
            ],
            "environment_distribution": {(env or "unset"): n for env, n in env_rows},
        }
        job.rows_read = stats.rows_read
        job.progress_message = "Import complete"
        record_audit(
            db,
            action="rvtools.import_job.completed",
            actor=job.actor,
            resource_type="import_job",
            resource_id=job.id,
            details={
                "filename": job.filename,
                "sha256": job.file_sha256,
                "mode": job.mode,
                "created": job.created_count,
                "updated": job.updated_count,
                "unchanged": job.unchanged_count,
                "marked_missing": job.marked_missing_count,
                "rejected": job.rows_rejected,
                "warned": job.rows_warned,
            },
        )
        _finish(db, job, "completed")
    except _Cancelled:
        db.rollback()
        job = db.get(ImportJob, job.id)
        job.progress_message = "Cancelled — batches committed before the cancel are kept"
        _finish(db, job, "cancelled")
    except RVToolsParseError as e:
        db.rollback()
        _finish(db, db.get(ImportJob, job.id), "failed", str(e))
    except Exception as e:  # noqa: BLE001 — a job must always reach a terminal state
        logger.exception("import %s crashed", job.id)
        db.rollback()
        _finish(
            db,
            db.get(ImportJob, job.id),
            "failed",
            f"Unexpected error: {e}. Batches committed before the failure are kept; "
            "re-upload to finish (re-import is idempotent).",
        )


# --------------------------------------------------------------------------
# Startup recovery
# --------------------------------------------------------------------------
def fail_orphan_imports(session_factory=None) -> int:
    """In-process BackgroundTasks don't survive a restart. Mark anything
    that was mid-flight as failed, and anything waiting on routing whose
    spool file is gone (emptyDir was wiped) likewise."""
    db: Session = (session_factory or _db_module.SessionLocal)()
    try:
        rows = list(
            db.scalars(
                select(ImportJob).where(
                    ImportJob.status.in_((*IMPORT_ACTIVE_STATUSES, "awaiting_mapping"))
                )
            ).all()
        )
        n = 0
        for job in rows:
            if job.status == "awaiting_mapping" and job.file_path and Path(job.file_path).exists():
                continue
            _finish(db, job, "failed", _ORPHAN_MESSAGE)
            n += 1
        return n
    finally:
        db.close()
