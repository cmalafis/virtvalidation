"""Bulk baseline-capture orchestrator.

Single-VM capture is already wired (``app.core.capture``); this
module batches that pipeline across an operator-selected fleet:

  - Resolves the scope filter (vcenter / app / env / status / search /
    explicit vm_ids) to a concrete list of VMs.
  - Runs each VM through the existing :func:`collect_and_store`
    code path concurrently, bounded by two semaphores:
      * a global ``max_parallel`` cap on total SSH sockets in flight
      * a per-vCenter cap so one vCenter source can't starve another
  - Continues on per-VM failures so one wedged host doesn't abort
    the rest of the batch.
  - Emits per-VM progress into the task store so the UI can render
    "5 of 87 complete · current: db-03.corp · 2 failed".

No LLM calls anywhere — baseline is pure data collection.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.audit import record_audit
from app.core.capture import CaptureError, collect_and_store
from app.core.config import settings
from app.models.vm import VM

logger = logging.getLogger(__name__)


# Default rate limits — tunable via the trigger endpoint and via
# Settings env vars for site-wide policy.
DEFAULT_MAX_PARALLEL = 10
DEFAULT_PER_VCENTER_PARALLEL = 5


# ---------------------------------------------------------------------------
# Task store
# ---------------------------------------------------------------------------
BulkCaptureStatus = Literal["running", "completed", "failed"]


@dataclass
class VMResult:
    vm_id: int
    vm_name: str
    status: Literal["queued", "capturing", "completed", "failed"]
    error: Optional[str] = None
    elapsed_seconds: Optional[int] = None


@dataclass
class BulkCaptureTask:
    task_id: str
    status: BulkCaptureStatus = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    current_vm: Optional[str] = None
    per_vm: dict[int, VMResult] = field(default_factory=dict)
    max_parallel: int = DEFAULT_MAX_PARALLEL
    per_vcenter_parallel: int = DEFAULT_PER_VCENTER_PARALLEL

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("started_at", "completed_at"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        # Convert per_vm to a list so the JSON response is shaped
        # for direct UI consumption.
        d["per_vm"] = [asdict(r) for r in self.per_vm.values()]
        return d


class BulkCaptureTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, BulkCaptureTask] = {}
        self._lock = threading.RLock()

    def create(
        self, *, total: int, max_parallel: int, per_vcenter_parallel: int
    ) -> BulkCaptureTask:
        task = BulkCaptureTask(
            task_id=str(uuid.uuid4()),
            total=total,
            max_parallel=max_parallel,
            per_vcenter_parallel=per_vcenter_parallel,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[BulkCaptureTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def set_vm_status(
        self,
        task_id: str,
        *,
        vm_id: int,
        vm_name: str,
        status: str,
        error: Optional[str] = None,
        elapsed_seconds: Optional[int] = None,
    ) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            entry = t.per_vm.get(vm_id) or VMResult(
                vm_id=vm_id, vm_name=vm_name, status="queued"
            )
            entry.status = status  # type: ignore[assignment]
            if error is not None:
                entry.error = error
            if elapsed_seconds is not None:
                entry.elapsed_seconds = elapsed_seconds
            t.per_vm[vm_id] = entry
            if status == "capturing":
                t.current_vm = vm_name
            if status == "completed":
                t.completed += 1
            if status == "failed":
                t.failed += 1

    def finalize(self, task_id: str, *, status: BulkCaptureStatus) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = status
            t.completed_at = datetime.now(timezone.utc)
            t.current_vm = None


task_store = BulkCaptureTaskStore()


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------
async def _run_one(
    semaphore: asyncio.Semaphore,
    vcenter_sema: Optional[asyncio.Semaphore],
    *,
    task_id: str,
    vm_id: int,
    vm_name: str,
    actor: str,
) -> None:
    """Run one VM's capture under the global + per-vCenter semaphores.

    ``collect_and_store`` is sync (paramiko has no asyncio wrapper).
    We invoke it via ``asyncio.to_thread`` so the worker stays inside
    the event loop without spawning a process pool.
    """
    async with semaphore:
        # Per-vCenter sema is None when the VM has no vcenter; in that
        # case we only honor the global cap.
        if vcenter_sema is not None:
            async with vcenter_sema:
                await _capture_in_thread(task_id, vm_id, vm_name, actor)
        else:
            await _capture_in_thread(task_id, vm_id, vm_name, actor)


async def _capture_in_thread(
    task_id: str, vm_id: int, vm_name: str, actor: str
) -> None:
    task_store.set_vm_status(
        task_id, vm_id=vm_id, vm_name=vm_name, status="capturing"
    )
    started = time.monotonic()
    try:
        await asyncio.to_thread(_capture_in_sync_thread, vm_id, actor)
        task_store.set_vm_status(
            task_id,
            vm_id=vm_id,
            vm_name=vm_name,
            status="completed",
            elapsed_seconds=int(time.monotonic() - started),
        )
    except Exception as e:  # noqa: BLE001 — we surface every failure
        logger.warning("bulk capture failed for vm %s: %s", vm_name, e)
        task_store.set_vm_status(
            task_id,
            vm_id=vm_id,
            vm_name=vm_name,
            status="failed",
            error=str(e),
            elapsed_seconds=int(time.monotonic() - started),
        )


def _capture_in_sync_thread(vm_id: int, actor: str) -> None:
    """Sync body that runs inside ``asyncio.to_thread``.

    Opens its own DB session because BackgroundTask threads don't
    share the request's session. Mirrors the pattern in
    :func:`run_capture_task`.
    """
    db = _db_module.SessionLocal()
    try:
        vm = db.get(VM, vm_id)
        if vm is None:
            raise CaptureError(f"vm_id {vm_id} not found")
        collect_and_store(db, vm, actor=actor)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def run_bulk_capture_async(
    task_id: str,
    *,
    vms: list[VM],
    actor: str,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    per_vcenter_parallel: int = DEFAULT_PER_VCENTER_PARALLEL,
) -> None:
    """Drive a bulk-capture run to completion.

    The task store row is created by the trigger endpoint; this
    function just walks the VM list and updates per-VM status.
    """
    global_sema = asyncio.Semaphore(max(1, max_parallel))
    per_vc_semas: dict[int, asyncio.Semaphore] = {}

    def _vc_sema_for(vc_id: Optional[int]) -> Optional[asyncio.Semaphore]:
        if vc_id is None:
            return None
        if vc_id not in per_vc_semas:
            per_vc_semas[vc_id] = asyncio.Semaphore(max(1, per_vcenter_parallel))
        return per_vc_semas[vc_id]

    coros = [
        _run_one(
            global_sema,
            _vc_sema_for(vm.source_vcenter_id),
            task_id=task_id,
            vm_id=vm.id,
            vm_name=vm.name,
            actor=actor,
        )
        for vm in vms
    ]
    await asyncio.gather(*coros, return_exceptions=False)
    # Even when individual VMs fail, the bulk task itself is
    # "completed" — failures show up in per_vm[].status.
    task_store.finalize(task_id, status="completed")

    # One audit row for the whole bulk run summarizing counts.
    db = _db_module.SessionLocal()
    try:
        task = task_store.get(task_id)
        if task is not None:
            record_audit(
                db,
                action="capture.bulk_completed",
                actor=actor,
                resource_type="bulk_capture",
                resource_id=None,
                details={
                    "task_id": task_id,
                    "total": task.total,
                    "completed": task.completed,
                    "failed": task.failed,
                    "max_parallel": task.max_parallel,
                    "per_vcenter_parallel": task.per_vcenter_parallel,
                },
            )
            db.commit()
    finally:
        db.close()


def run_bulk_capture(
    task_id: str,
    *,
    vm_ids: list[int],
    actor: str,
    max_parallel: int = DEFAULT_MAX_PARALLEL,
    per_vcenter_parallel: int = DEFAULT_PER_VCENTER_PARALLEL,
) -> None:
    """Sync entry point for FastAPI BackgroundTasks. Wraps the async
    orchestrator in :func:`asyncio.run` and resolves vm_ids → VM
    rows inside a fresh session."""
    db = _db_module.SessionLocal()
    try:
        vms = list(db.query(VM).filter(VM.id.in_(vm_ids)).all())
    finally:
        db.close()
    # Pre-populate per_vm entries so the UI sees the full list
    # immediately, not just rows that have started.
    for vm in vms:
        task_store.set_vm_status(
            task_id, vm_id=vm.id, vm_name=vm.name, status="queued"
        )
    try:
        asyncio.run(
            run_bulk_capture_async(
                task_id,
                vms=vms,
                actor=actor,
                max_parallel=max_parallel,
                per_vcenter_parallel=per_vcenter_parallel,
            )
        )
    except Exception as e:  # pragma: no cover — defensive
        logger.exception("bulk capture task %s crashed", task_id)
        task_store.finalize(task_id, status="failed")
        raise
