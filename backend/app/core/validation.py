"""On-demand post-migration validation.

Mirrors ``app.core.capture`` so the manual-validation flow looks the same
as the manual-capture flow from the frontend's perspective:

  - ``run_validation_task(task_id, vm_id, *, actor)`` — body of a
    FastAPI BackgroundTask. Walks the full SSH-collect → diff → LLM
    reason → persist pipeline, updating the in-memory ``task_store``
    after each phase so the UI's poll endpoint can render progress.

  - ``ValidationTaskStore`` — thread-safe ephemeral registry. Same
    "evaporates on restart, UI refreshes inventory to reconcile"
    contract as ``CaptureTaskStore``.

The actual SSH collection reuses ``SSHCollector`` and the LLM call
reuses ``LLMClient`` — this module only owns orchestration, progress
reporting, and audit.
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.audit import record_audit
from app.core.baseline import synthesize_profile
from app.core.capture import (
    CaptureError,
    _audit_host_key_event,
    _audit_host_key_failure,
    _resolve_host_key_policy,
)
from app.core.config import settings
from app.core.llm import LLMClient, LLMError
from app.core.ssh import SSHCollectionError, SSHCollector
from app.models.validation import ValidationResult, ValidationStatus
from app.models.vm import VM, BaselineSnapshot, VMStatus

logger = logging.getLogger(__name__)

ValidationStatusLiteral = Literal["running", "completed", "failed"]
CurrentStep = Literal[
    "queued",
    "ssh_collecting",
    "llm_reasoning",
    "storing",
    "completed",
    "failed",
]

# Verdict ↔ VM status mapping. The post-validation VM status reflects the
# verdict the operator just saw in the UI so the inventory list at a glance
# still tells the right story.
_VERDICT_TO_VM_STATUS: dict[str, VMStatus] = {
    "pass": VMStatus.validated,
    "warn": VMStatus.validated,
    "fail": VMStatus.failed,
}

# JSON status from the LLM ↔ ValidationStatus enum stored on the row.
_VERDICT_TO_ENUM: dict[str, ValidationStatus] = {
    "pass": ValidationStatus.passed,
    "warn": ValidationStatus.warn,
    "fail": ValidationStatus.failed,
}


# ---------------------------------------------------------------------------
# Validation task registry
# ---------------------------------------------------------------------------
@dataclass
class ValidationTask:
    task_id: str
    vm_id: int
    status: ValidationStatusLiteral = "running"
    current_step: CurrentStep = "queued"
    progress_percent: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    validation_id: Optional[int] = None
    verdict: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("started_at", "completed_at"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        return d


class ValidationTaskStore:
    """Thread-safe in-memory registry of running validation tasks."""

    def __init__(self) -> None:
        self._tasks: dict[str, ValidationTask] = {}
        self._lock = threading.RLock()

    def create(self, vm_id: int) -> ValidationTask:
        task = ValidationTask(task_id=str(uuid.uuid4()), vm_id=vm_id)
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[ValidationTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def update_step(
        self,
        task_id: str,
        *,
        current_step: CurrentStep,
        progress_percent: int,
    ) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.current_step = current_step
            t.progress_percent = progress_percent

    def mark_completed(
        self,
        task_id: str,
        *,
        validation_id: int,
        verdict: str,
    ) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "completed"
            t.current_step = "completed"
            t.progress_percent = 100
            t.validation_id = validation_id
            t.verdict = verdict
            t.completed_at = datetime.now(timezone.utc)

    def mark_failed(self, task_id: str, *, error: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "failed"
            t.current_step = "failed"
            t.error = error
            t.completed_at = datetime.now(timezone.utc)


task_store = ValidationTaskStore()


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
class ValidationError(RuntimeError):
    """Raised inside the workflow when collection, reasoning, or persistence fails."""


def _baseline_profile_dict(db: Session, vm: VM) -> Optional[dict]:
    """Return the synthesized baseline as a plain dict, or None if no snapshots."""
    snapshots = list(
        db.scalars(
            select(BaselineSnapshot)
            .where(BaselineSnapshot.vm_id == vm.id)
            .order_by(BaselineSnapshot.collected_at.asc())
        ).all()
    )
    if not snapshots:
        return None
    synthesize_profile(vm.id, snapshots)
    # The LLM diff engine works on the raw shape (services/ports/mounts/network/cron).
    # Use the most recent raw snapshot as the structural baseline; the synthesized
    # profile is a summary, not a diff source.
    return snapshots[-1].raw_data or {}


def _collect_current_state(
    db: Session,
    vm: VM,
    *,
    actor: str,
    collector: Optional[SSHCollector] = None,
) -> dict:
    """SSH into the VM and return its current state dict.

    Audit security-relevant host-key events the same way ``collect_and_store``
    does — the validation flow is just as load-bearing for federal review.
    """
    if collector is None:
        collector = SSHCollector(
            key_path=settings.ssh_key_path,
            host_key_policy=_resolve_host_key_policy(db),
        )

    host = vm.ip_address or vm.source_hostname
    if not host:
        raise ValidationError(f"VM {vm.name} has no host or IP address to SSH to")

    ssh_user = vm.ssh_user or "virtvalidate"

    try:
        state = collector.collect(host=host, username=ssh_user)
    except SSHCollectionError as e:
        _audit_host_key_failure(db, actor=actor, vm=vm, err=e)
        raise ValidationError(str(e)) from e

    _audit_host_key_event(db, actor=actor, vm=vm, event=collector.last_host_key_event)
    return state


def run_validation(
    db: Session,
    vm: VM,
    *,
    actor: str,
    collector: Optional[SSHCollector] = None,
    llm_client: Optional[LLMClient] = None,
    progress: Optional[ValidationTaskStore] = None,
    task_id: Optional[str] = None,
) -> ValidationResult:
    """Drive one VM through the full validation pipeline.

    The optional ``progress`` + ``task_id`` arguments let the BackgroundTask
    wrapper bubble per-phase progress into the in-memory task store; the
    bulk ``run-all`` path invokes this synchronously without progress updates.
    """

    def _step(step: CurrentStep, percent: int) -> None:
        if progress is not None and task_id is not None:
            progress.update_step(task_id, current_step=step, progress_percent=percent)

    baseline = _baseline_profile_dict(db, vm)
    if baseline is None:
        raise ValidationError("Capture a baseline first before running validation")

    _step("ssh_collecting", 30)
    try:
        current = _collect_current_state(db, vm, actor=actor, collector=collector)
    except ValidationError:
        raise
    except CaptureError as e:  # defensive — _collect_current_state raises ValidationError
        raise ValidationError(str(e)) from e

    _step("llm_reasoning", 60)
    client = llm_client or LLMClient()
    try:
        verdict = client.validate(baseline, current, vm_role=vm.role or "")
    except LLMError as e:
        raise ValidationError(f"LLM reasoning failed: {e}") from e

    status_str = verdict.get("status", "warn")
    if status_str not in _VERDICT_TO_ENUM:
        raise ValidationError(f"Invalid verdict status from LLM: {status_str!r}")

    _step("storing", 90)
    row = ValidationResult(
        vm_id=vm.id,
        status=_VERDICT_TO_ENUM[status_str],
        summary=verdict.get("summary", ""),
        findings=verdict.get("findings", []) or [],
        remediation=verdict.get("remediation", []) or [],
        diff=verdict.get("diff", {}) or {},
    )
    db.add(row)

    new_status = _VERDICT_TO_VM_STATUS.get(status_str)
    if new_status is not None:
        vm.status = new_status

    db.commit()
    db.refresh(row)

    record_audit(
        db,
        action="validation.completed",
        actor=actor,
        resource_type="validation",
        resource_id=row.id,
        details={
            "vm_id": vm.id,
            "vm_name": vm.name,
            "verdict": status_str,
            "finding_count": len(row.findings),
        },
    )
    db.commit()
    return row


# ---------------------------------------------------------------------------
# BackgroundTask entry point
# ---------------------------------------------------------------------------
def run_validation_task(task_id: str, vm_id: int, *, actor: str = "user") -> None:
    """Body of the FastAPI BackgroundTask spawned by the manual-validate endpoint."""
    db = _db_module.SessionLocal()
    try:
        vm = db.get(VM, vm_id)
        if vm is None:
            task_store.mark_failed(task_id, error=f"VM {vm_id} not found")
            return

        task_store.update_step(task_id, current_step="ssh_collecting", progress_percent=10)
        try:
            row = run_validation(
                db,
                vm,
                actor=actor,
                progress=task_store,
                task_id=task_id,
            )
        except ValidationError as e:
            logger.warning("validation failed for VM %s: %s", vm.name, e)
            task_store.mark_failed(task_id, error=str(e))
            return

        task_store.mark_completed(
            task_id,
            validation_id=row.id,
            verdict=row.status.value,
        )
        logger.info(
            "validation %s stored for VM %s (verdict=%s, task %s)",
            row.id,
            vm.name,
            row.status.value,
            task_id,
        )
    finally:
        db.close()
