"""On-demand baseline capture.

Two responsibilities, kept here so they can't drift apart:

  - ``collect_and_store(db, vm, *, actor)`` — the canonical "capture one
    baseline" routine. Both the scheduler and the manual-trigger
    endpoints call this; the only thing they vary is the audit ``actor``
    string.

  - ``CaptureTaskStore`` — a thread-safe, in-memory registry of running
    capture tasks. Manual triggers spawn FastAPI BackgroundTasks; the
    store lets the status endpoint look up progress without persisting
    short-lived task state into the database.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import db as _db_module  # late-binding so tests can rebind SessionLocal
from app.core.audit import record_audit
from app.core.config import settings
from app.core.ssh import HostKeyPolicy, SSHCollectionError, SSHCollector
from app.models.settings import AppSettings, SSHHostKeyPolicy
from app.models.vm import VM, BaselineSnapshot, VMStatus

logger = logging.getLogger(__name__)

CaptureStatus = Literal["running", "completed", "failed"]


# ---------------------------------------------------------------------------
# Capture task registry — ephemeral, in-memory.
# ---------------------------------------------------------------------------
@dataclass
class CaptureTask:
    task_id: str
    vm_id: int
    status: CaptureStatus = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    snapshot_id: Optional[int] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        # ISO-format timestamps for JSON output.
        for key in ("started_at", "completed_at"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        return d


class CaptureTaskStore:
    """Thread-safe singleton-ish task registry.

    BackgroundTasks run in a thread pool; multiple concurrent captures
    are expected. The store uses an RLock so reads + writes are serialized
    without deadlocking on re-entrant calls.

    Tasks are kept in-memory only — they evaporate on process restart.
    The user-facing UI re-polls and accepts that "task not found" after
    a restart means "treat as completed; refresh inventory to confirm".
    """

    def __init__(self) -> None:
        self._tasks: dict[str, CaptureTask] = {}
        self._lock = threading.RLock()

    def create(self, vm_id: int) -> CaptureTask:
        task = CaptureTask(task_id=str(uuid.uuid4()), vm_id=vm_id)
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[CaptureTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def mark_completed(self, task_id: str, *, snapshot_id: int) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "completed"
            t.snapshot_id = snapshot_id
            t.completed_at = datetime.now(timezone.utc)

    def mark_failed(self, task_id: str, *, error: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "failed"
            t.error = error
            t.completed_at = datetime.now(timezone.utc)


# Single process-wide store. Imported by the API and the BackgroundTask
# entry point so they see the same dict.
task_store = CaptureTaskStore()


# ---------------------------------------------------------------------------
# Shared collection routine.
# ---------------------------------------------------------------------------
class CaptureError(RuntimeError):
    """Raised by collect_and_store when SSH or persistence fails."""


def _resolve_host_key_policy(db: Session) -> HostKeyPolicy:
    """Read the saved policy from the singleton AppSettings row.

    Defaults to ``auto_accept`` when the row hasn't been initialized yet
    — the same default used in the model's column.
    """
    row = db.get(AppSettings, 1)
    if row is None:
        return "auto_accept"
    return "strict" if row.ssh_host_key_policy == SSHHostKeyPolicy.strict else "auto_accept"


def _audit_host_key_event(
    db: Session,
    *,
    actor: str,
    vm: VM,
    event: dict | None,
) -> None:
    """Write an audit row for the SSH host-key acceptance, if any.

    The collector reports a ``{"action": "added"|"verified", ...}`` event
    after each connect. We only audit ``added`` (TOFU acceptances) — the
    ``verified`` path runs on every scheduled poll and would drown the
    trail. Verification *failures* don't come through here; they raise
    ``SSHCollectionError(kind="host_key_mismatch")`` and are audited by
    ``_audit_host_key_failure`` instead.
    """
    if not event or event.get("action") != "added":
        return
    record_audit(
        db,
        action="ssh.host_key_accepted",
        actor=actor,
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "host": event.get("host"),
            "key_type": event.get("key_type"),
            "fingerprint": event.get("fingerprint"),
        },
    )
    db.commit()


def _audit_host_key_failure(
    db: Session,
    *,
    actor: str,
    vm: VM,
    err: SSHCollectionError,
) -> None:
    """Audit security-relevant SSH host-key failures.

    Called for ``host_key_mismatch`` (stored key doesn't match — possible
    MITM) and ``host_key_unknown`` (strict mode hit). Both are loud
    failures the federal compliance reviewer cares about; ``generic``
    failures (auth, network) are routine and stay out of the audit log
    here so we don't bury the signal.
    """
    if err.kind not in {"host_key_mismatch", "host_key_unknown"}:
        return
    record_audit(
        db,
        action=f"ssh.{err.kind}",
        actor=actor,
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "host": err.host,
            "fingerprint": err.fingerprint,
            "message": str(err),
        },
    )
    db.commit()


def collect_and_store(
    db: Session,
    vm: VM,
    *,
    actor: str,
    collector: Optional[SSHCollector] = None,
) -> BaselineSnapshot:
    """SSH into ``vm``, store a fresh BaselineSnapshot, audit, return it.

    The audit ``actor`` is the only thing that varies between scheduled
    and manual captures — everything else (command set, snapshot numbering,
    VM status transition) is identical.

    Raises ``CaptureError`` on SSH failure so callers can mark the task
    or scheduler iteration appropriately.
    """
    if collector is None:
        collector = SSHCollector(
            key_path=settings.ssh_key_path,
            host_key_policy=_resolve_host_key_policy(db),
        )

    host = vm.ip_address or vm.source_hostname
    if not host:
        raise CaptureError(f"VM {vm.name} has no host or IP address to SSH to")

    ssh_user = vm.ssh_user or "virtvalidate"

    try:
        state = collector.collect(host=host, username=ssh_user)
    except SSHCollectionError as e:
        # Audit security-relevant host key failures BEFORE re-raising so
        # the trail captures them even on the failure path.
        _audit_host_key_failure(db, actor=actor, vm=vm, err=e)
        raise CaptureError(str(e)) from e

    # First-time host key acceptance lands in the audit log alongside the
    # baseline.collected row. Together they tell the reviewer "VirtValidate
    # accepted this host's key for the first time, then captured a baseline."
    _audit_host_key_event(db, actor=actor, vm=vm, event=collector.last_host_key_event)

    next_number = (
        db.scalar(
            select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0)).where(
                BaselineSnapshot.vm_id == vm.id
            )
        )
        or 0
    ) + 1

    snapshot = BaselineSnapshot(
        vm_id=vm.id,
        snapshot_number=next_number,
        ssh_user=ssh_user,
        raw_data=state,
    )
    db.add(snapshot)
    if vm.status == VMStatus.discovered:
        vm.status = VMStatus.baseline_captured
    db.commit()
    db.refresh(snapshot)

    record_audit(
        db,
        action="baseline.collected",
        actor=actor,
        resource_type="baseline",
        resource_id=snapshot.id,
        details={
            "vm_id": vm.id,
            "vm_name": vm.name,
            "snapshot_number": next_number,
            "ssh_user": ssh_user,
        },
    )
    db.commit()
    return snapshot


# ---------------------------------------------------------------------------
# BackgroundTask entry point.
# ---------------------------------------------------------------------------
def run_capture_task(task_id: str, vm_id: int, *, actor: str = "user") -> None:
    """Body of the FastAPI BackgroundTask spawned by manual-capture endpoints.

    Opens its own DB session because BackgroundTasks run after the request
    handler has returned and its session has closed. Updates the
    ``task_store`` so the status endpoint can report progress.
    """
    db = _db_module.SessionLocal()
    try:
        vm = db.get(VM, vm_id)
        if vm is None:
            task_store.mark_failed(task_id, error=f"VM {vm_id} not found")
            return
        try:
            snapshot = collect_and_store(db, vm, actor=actor)
        except CaptureError as e:
            logger.warning("manual capture failed for VM %s: %s", vm.name, e)
            task_store.mark_failed(task_id, error=str(e))
            return
        task_store.mark_completed(task_id, snapshot_id=snapshot.id)
        logger.info(
            "manual capture #%d stored for VM %s (task %s)",
            snapshot.snapshot_number,
            vm.name,
            task_id,
        )
    finally:
        db.close()
