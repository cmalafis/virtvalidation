"""Cross-VM validation operations — currently bulk on-demand validate."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import get_db
from app.core.validation import run_validation_task, task_store
from app.models.vm import VM, BaselineSnapshot
from app.schemas.validation import BulkValidationResult

router = APIRouter(tags=["validations"])


@router.post("/run-all", response_model=BulkValidationResult)
def validate_all(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn a validation against every enrolled VM that has a baseline.

    VMs without a baseline are skipped — running the LLM on a VM with no
    prior state would just produce a "no baseline" finding, which is more
    useful surfaced as a skip reason here.
    """
    actor = request.headers.get("x-actor", "user")

    vms = list(db.scalars(select(VM)).all())
    # One round-trip to pre-compute which VMs have at least one snapshot.
    vm_ids_with_baselines = set(db.scalars(select(BaselineSnapshot.vm_id).distinct()).all())

    spawned: list[dict] = []
    skipped: list[dict] = []

    for vm in vms:
        if vm.id not in vm_ids_with_baselines:
            skipped.append({"vm_id": vm.id, "reason": "no baseline captured"})
            continue
        if not (vm.ip_address or vm.source_hostname):
            skipped.append({"vm_id": vm.id, "reason": "no host or IP address"})
            continue
        task = task_store.create(vm_id=vm.id)
        spawned.append({"vm_id": vm.id, "task_id": task.task_id})
        record_audit(
            db,
            action="validation.triggered",
            actor=actor,
            resource_type="vm",
            resource_id=vm.id,
            details={
                "vm_name": vm.name,
                "task_id": task.task_id,
                "via": "bulk",
            },
        )
        background_tasks.add_task(run_validation_task, task.task_id, vm.id, actor=actor)

    db.commit()
    request.state.skip_audit_log = True
    return {"spawned": spawned, "skipped": skipped}
