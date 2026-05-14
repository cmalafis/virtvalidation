"""Cross-VM validation operations: legacy ``run-all`` (per-VM tasks)
and the new tier-aware ``run-bulk`` + ``preview-tiers`` surfaces.

The new endpoints power the validation-selection UI: operators filter
to a scope (vCenter / app / env / wave), preview the tier distribution
(estimated LLM call count), then submit. Progress streams via the
shared :class:`BulkValidationTaskStore`.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.bulk_validation import (
    preview_tier_distribution,
    run_bulk_validation,
)
from app.core.bulk_validation import task_store as bulk_task_store
from app.core.db import get_db
from app.core.limits import MAX_VMS_PER_BULK_ACTION
from app.core.validation import run_validation_task, task_store
from app.models.vm import VM, BaselineSnapshot
from app.schemas.validation import BulkValidationResult

router = APIRouter(tags=["validations"])


# ---------------------------------------------------------------------------
# Selection schema reused by run-bulk + preview-tiers.
# ---------------------------------------------------------------------------
class BulkValidationScope(BaseModel):
    """Selection axes the operator can stack. At least one must match
    a non-empty set or the request returns 422."""

    vm_ids: list[int] = Field(default_factory=list, max_length=MAX_VMS_PER_BULK_ACTION)
    source_vcenter_id: int | None = None
    environment: str | None = None
    application_hint: str | None = None
    only_migrated: bool = False
    not_validated_within_hours: int | None = None
    use_cache: bool = True


def _resolve_scope(db: Session, scope: BulkValidationScope) -> list[VM]:
    if scope.vm_ids:
        return list(db.scalars(select(VM).where(VM.id.in_(scope.vm_ids)).order_by(VM.id)).all())
    stmt = select(VM).order_by(VM.id)
    if scope.source_vcenter_id is not None:
        stmt = stmt.where(VM.source_vcenter_id == scope.source_vcenter_id)
    if scope.environment:
        stmt = stmt.where(VM.environment == scope.environment)
    if scope.application_hint:
        stmt = stmt.where(VM.application_hint == scope.application_hint)
    return list(db.scalars(stmt).all())


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


# ---------------------------------------------------------------------------
# Tier-aware bulk validation
# ---------------------------------------------------------------------------
@router.post("/preview-tiers")
def preview_tiers(payload: BulkValidationScope, db: Session = Depends(get_db)) -> dict:
    """Estimate the LLM-call cost of a bulk validation before running.

    Walks each matched VM through SSH-collect + diff + classifier (no
    LLM call) and reports how many would land in Tier 1 (no LLM),
    Tier 2 (rules, no LLM), or Tier 3 (LLM). Surfaces a per-VM error
    list for VMs that can't be previewed (e.g. no baseline).
    """
    matched = _resolve_scope(db, payload)
    if not matched:
        raise HTTPException(status_code=422, detail="Scope matched zero VMs")
    return preview_tier_distribution(db, vm_ids=[vm.id for vm in matched], actor="preview")


@router.post("/run-bulk", status_code=202)
def run_bulk(
    request: Request,
    payload: BulkValidationScope,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn a tier-aware bulk validation.

    Returns 202 with a task handle the UI polls. Tier 1 / 2 VMs
    complete instantly (no LLM call); Tier 3 VMs hit the cache or
    the LLM and report cached vs fresh in the per-VM status.
    """
    actor = request.headers.get("x-actor", "user")
    matched = _resolve_scope(db, payload)
    if not matched:
        raise HTTPException(status_code=422, detail="Scope matched zero VMs")

    # Filter out VMs without a baseline — validation requires one.
    vm_ids_with_baselines = set(db.scalars(select(BaselineSnapshot.vm_id).distinct()).all())
    validatable = [vm for vm in matched if vm.id in vm_ids_with_baselines]
    skipped = [
        {"vm_id": vm.id, "reason": "no baseline captured"}
        for vm in matched
        if vm.id not in vm_ids_with_baselines
    ]
    if not validatable:
        raise HTTPException(
            status_code=422,
            detail=(
                f"None of the {len(matched)} matched VMs have a baseline. " "Run capture first."
            ),
        )

    task = bulk_task_store.create(total=len(validatable))
    record_audit(
        db,
        action="validation.bulk_triggered",
        actor=actor,
        resource_type="bulk_validation",
        resource_id=None,
        details={
            "task_id": task.task_id,
            "total": len(validatable),
            "skipped": len(skipped),
            "use_cache": payload.use_cache,
        },
    )
    db.commit()

    background_tasks.add_task(
        run_bulk_validation,
        task.task_id,
        vm_ids=[vm.id for vm in validatable],
        actor=actor,
        use_cache=payload.use_cache,
    )
    request.state.skip_audit_log = True
    return {
        "task_id": task.task_id,
        "total": len(validatable),
        "skipped": skipped,
    }


@router.get("/run-bulk/{task_id}")
def get_bulk_validation_status(task_id: str) -> dict:
    task = bulk_task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Bulk validation task {task_id} not found. Tasks live "
                "in memory; restart the appliance clears them."
            ),
        )
    return task.to_dict()
