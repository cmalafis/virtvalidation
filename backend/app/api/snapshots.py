"""Cross-VM snapshot operations — currently just bulk on-demand capture."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.capture import run_capture_task, task_store
from app.core.db import get_db
from app.models.vm import VM
from app.schemas.vm import BulkCaptureResult

router = APIRouter(tags=["snapshots"])


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
