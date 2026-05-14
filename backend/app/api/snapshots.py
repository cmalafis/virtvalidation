"""Cross-VM snapshot operations: legacy ``capture-all`` (per-VM tasks)
and the new selection-driven ``capture-bulk`` with global + per-vCenter
rate limiting.

The new endpoint exists alongside the old ``capture-all`` for
back-compat with the existing dashboard button; new UI flows use
``capture-bulk`` with an explicit scope filter and runtime-tunable
parallelism.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.bulk_capture import (
    DEFAULT_MAX_PARALLEL,
    DEFAULT_PER_VCENTER_PARALLEL,
    run_bulk_capture,
)
from app.core.bulk_capture import task_store as bulk_task_store
from app.core.capture import run_capture_task, task_store
from app.core.db import get_db
from app.core.limits import MAX_VMS_PER_BULK_ACTION
from app.models.vm import VM
from app.schemas.vm import BulkCaptureResult

router = APIRouter(tags=["snapshots"])


# ---------------------------------------------------------------------------
# Bulk-capture selection
# ---------------------------------------------------------------------------
class BulkCaptureScope(BaseModel):
    """Selection axes for the bulk-capture endpoint. Stack any subset;
    leave all empty to capture every VM that has a host/IP."""

    vm_ids: list[int] = Field(default_factory=list, max_length=MAX_VMS_PER_BULK_ACTION)
    source_vcenter_id: int | None = None
    environment: str | None = None
    application_hint: str | None = None
    only_status: str | None = None  # e.g. "discovered"
    name_contains: str | None = None
    max_parallel: int = Field(default=DEFAULT_MAX_PARALLEL, ge=1, le=100)
    per_vcenter_parallel: int = Field(default=DEFAULT_PER_VCENTER_PARALLEL, ge=1, le=50)


def _resolve_capture_scope(db: Session, scope: BulkCaptureScope) -> list[VM]:
    if scope.vm_ids:
        return list(db.scalars(select(VM).where(VM.id.in_(scope.vm_ids)).order_by(VM.id)).all())
    stmt = select(VM).order_by(VM.id)
    if scope.source_vcenter_id is not None:
        stmt = stmt.where(VM.source_vcenter_id == scope.source_vcenter_id)
    if scope.environment:
        stmt = stmt.where(VM.environment == scope.environment)
    if scope.application_hint:
        stmt = stmt.where(VM.application_hint == scope.application_hint)
    if scope.only_status:
        stmt = stmt.where(VM.status == scope.only_status)
    if scope.name_contains:
        stmt = stmt.where(VM.name.ilike(f"%{scope.name_contains}%"))
    return list(db.scalars(stmt).all())


@router.post("/capture-all", response_model=BulkCaptureResult)
def capture_all(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn an immediate capture against every enrolled VM.

    Returns the list of (vm_id, task_id) handles the dashboard polls.
    VMs without a host or IP address are skipped — they would fail the
    SSH leg anyway, and surfacing them up-front beats burying the failure
    in per-task error fields.
    """
    actor = request.headers.get("x-actor", "user")

    vms = list(db.scalars(select(VM)).all())
    spawned: list[dict] = []
    skipped: list[dict] = []

    for vm in vms:
        if not (vm.ip_address or vm.source_hostname):
            skipped.append({"vm_id": vm.id, "reason": "no host or IP address"})
            continue
        task = task_store.create(vm_id=vm.id)
        spawned.append({"vm_id": vm.id, "task_id": task.task_id})
        record_audit(
            db,
            action="capture.triggered",
            actor=actor,
            resource_type="vm",
            resource_id=vm.id,
            details={
                "vm_name": vm.name,
                "task_id": task.task_id,
                "via": "bulk",
            },
        )
        background_tasks.add_task(run_capture_task, task.task_id, vm.id, actor=actor)

    db.commit()
    request.state.skip_audit_log = True
    return {"spawned": spawned, "skipped": skipped}


# ---------------------------------------------------------------------------
# New: bulk capture with selection + rate limiting
# ---------------------------------------------------------------------------
@router.post("/capture-bulk", status_code=202)
def capture_bulk(
    request: Request,
    payload: BulkCaptureScope,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn a rate-limited bulk capture against a selected fleet.

    Returns 202 + task_id. Progress is polled via
    ``GET /api/snapshots/capture-bulk/{task_id}``. Per-VM failures
    surface in the per_vm list; the bulk task itself completes
    successfully even when individual VMs fail.
    """
    actor = request.headers.get("x-actor", "user")
    matched = _resolve_capture_scope(db, payload)
    if not matched:
        raise HTTPException(status_code=422, detail="Scope matched zero VMs")

    # Filter out unreachable VMs (no host + no IP). The bulk worker
    # would fail them anyway; surfacing them up front beats
    # surfacing failures in per-VM error fields.
    capturable: list[VM] = []
    skipped: list[dict] = []
    for vm in matched:
        if not (vm.ip_address or vm.source_hostname):
            skipped.append({"vm_id": vm.id, "reason": "no host or IP address"})
        else:
            capturable.append(vm)

    if not capturable:
        raise HTTPException(
            status_code=422,
            detail="All matched VMs lack a host or IP — nothing capturable",
        )

    task = bulk_task_store.create(
        total=len(capturable),
        max_parallel=payload.max_parallel,
        per_vcenter_parallel=payload.per_vcenter_parallel,
    )
    record_audit(
        db,
        action="capture.bulk_triggered",
        actor=actor,
        resource_type="bulk_capture",
        resource_id=None,
        details={
            "task_id": task.task_id,
            "total": len(capturable),
            "skipped": len(skipped),
            "max_parallel": payload.max_parallel,
            "per_vcenter_parallel": payload.per_vcenter_parallel,
        },
    )
    db.commit()

    background_tasks.add_task(
        run_bulk_capture,
        task.task_id,
        vm_ids=[vm.id for vm in capturable],
        actor=actor,
        max_parallel=payload.max_parallel,
        per_vcenter_parallel=payload.per_vcenter_parallel,
    )
    request.state.skip_audit_log = True
    return {
        "task_id": task.task_id,
        "total": len(capturable),
        "skipped": skipped,
    }


@router.get("/capture-bulk/{task_id}")
def get_bulk_capture_status(task_id: str) -> dict:
    task = bulk_task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Bulk capture task {task_id} not found. Tasks live in "
                "memory; an appliance restart clears them."
            ),
        )
    return task.to_dict()
