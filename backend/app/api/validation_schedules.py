"""CRUD for ValidationSchedule + an on-demand "fire now" endpoint.

The actual scheduling integration (APScheduler reading these rows at
startup + on cron tick) lands in ``app.core.scheduler``. The API
surface here lets operators create / pause / fire schedules without
restarting the appliance.

A schedule is just a saved scope filter + cron expression. Firing
the schedule runs the bulk-validation pipeline with the saved scope
— same code path as a manual bulk run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.bulk_validation import run_bulk_validation
from app.core.bulk_validation import task_store as bulk_task_store
from app.core.db import get_db
from app.core.limits import MAX_VMS_PER_BULK_ACTION
from app.models.validation_schedule import ScheduleStatus, ValidationSchedule
from app.models.vm import VM, BaselineSnapshot

router = APIRouter(tags=["validation-schedules"])


class ScheduleScope(BaseModel):
    """Same shape as :class:`BulkValidationScope` in
    :mod:`app.api.validations`, kept duplicated so the schedule row
    can store + replay it without a cross-API import."""

    vm_ids: list[int] = Field(default_factory=list, max_length=MAX_VMS_PER_BULK_ACTION)
    source_vcenter_id: int | None = None
    environment: str | None = None
    application_hint: str | None = None


class ScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    cron_expression: str = Field(min_length=1, max_length=64)
    timezone: str = Field(default="UTC", max_length=64)
    scope: ScheduleScope = Field(default_factory=ScheduleScope)
    notes: str | None = Field(default=None, max_length=4096)
    status: Literal["enabled", "paused", "disabled"] = "enabled"


class ScheduleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    cron_expression: str | None = Field(default=None, min_length=1, max_length=64)
    timezone: str | None = Field(default=None, max_length=64)
    scope: ScheduleScope | None = None
    notes: str | None = Field(default=None, max_length=4096)
    status: Literal["enabled", "paused", "disabled"] | None = None
    runtime_paused: bool | None = None


class ScheduleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    name: str
    cron_expression: str
    timezone: str
    scope: dict
    status: ScheduleStatus
    notes: str | None = None
    last_fired_at: datetime | None = None
    last_task_id: str | None = None
    next_fire_at: datetime | None = None
    runtime_paused: bool
    created_by_actor: str
    created_at: datetime
    updated_at: datetime


@router.get("", response_model=list[ScheduleRead])
def list_schedules(db: Session = Depends(get_db)) -> list[ValidationSchedule]:
    return list(
        db.scalars(select(ValidationSchedule).order_by(ValidationSchedule.created_at.desc())).all()
    )


@router.post(
    "",
    response_model=ScheduleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_schedule(
    request: Request,
    payload: ScheduleCreate,
    db: Session = Depends(get_db),
) -> ValidationSchedule:
    actor = request.headers.get("x-actor", "user")
    sched = ValidationSchedule(
        name=payload.name,
        cron_expression=payload.cron_expression,
        timezone=payload.timezone,
        scope=payload.scope.model_dump(),
        notes=payload.notes,
        status=ScheduleStatus(payload.status),
        created_by_actor=actor,
    )
    db.add(sched)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Schedule named {payload.name!r} already exists",
        ) from e
    db.refresh(sched)
    record_audit(
        db,
        action="validation_schedule.create",
        actor=actor,
        resource_type="validation_schedule",
        resource_id=sched.id,
        details={
            "name": sched.name,
            "cron": sched.cron_expression,
            "scope": sched.scope,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return sched


@router.get("/{schedule_id}", response_model=ScheduleRead)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)) -> ValidationSchedule:
    row = db.get(ValidationSchedule, schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
    return row


@router.patch("/{schedule_id}", response_model=ScheduleRead)
def update_schedule(
    request: Request,
    schedule_id: int,
    payload: ScheduleUpdate,
    db: Session = Depends(get_db),
) -> ValidationSchedule:
    row = db.get(ValidationSchedule, schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
    updates = payload.model_dump(exclude_unset=True)
    if "scope" in updates and updates["scope"] is not None:
        row.scope = updates.pop("scope")
    if "status" in updates and updates["status"] is not None:
        row.status = ScheduleStatus(updates.pop("status"))
    for field, value in updates.items():
        setattr(row, field, value)
    db.commit()
    db.refresh(row)
    request.state.skip_audit_log = True
    return row


@router.delete("/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_schedule(
    request: Request,
    schedule_id: int,
    db: Session = Depends(get_db),
) -> None:
    row = db.get(ValidationSchedule, schedule_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
    record_audit(
        db,
        action="validation_schedule.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="validation_schedule",
        resource_id=row.id,
        details={"name": row.name},
    )
    db.delete(row)
    db.commit()
    request.state.skip_audit_log = True


@router.post("/{schedule_id}/fire", status_code=202)
def fire_schedule(
    request: Request,
    schedule_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Trigger a schedule immediately, bypassing its cron timing.

    Same code path APScheduler invokes — useful for "validate now"
    one-off runs from the operator dashboard.
    """
    sched = db.get(ValidationSchedule, schedule_id)
    if sched is None:
        raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
    if sched.status == ScheduleStatus.disabled:
        raise HTTPException(status_code=409, detail="Schedule is disabled")

    actor = request.headers.get("x-actor", "user")
    scope = sched.scope or {}

    # Resolve the scope to VM ids that have a baseline.
    stmt = select(VM).order_by(VM.id)
    if scope.get("source_vcenter_id") is not None:
        stmt = stmt.where(VM.source_vcenter_id == scope["source_vcenter_id"])
    if scope.get("environment"):
        stmt = stmt.where(VM.environment == scope["environment"])
    if scope.get("application_hint"):
        stmt = stmt.where(VM.application_hint == scope["application_hint"])
    if scope.get("vm_ids"):
        stmt = stmt.where(VM.id.in_(scope["vm_ids"]))
    matched = list(db.scalars(stmt).all())
    if not matched:
        raise HTTPException(status_code=422, detail="Schedule scope matched zero VMs")

    vm_ids_with_baselines = set(db.scalars(select(BaselineSnapshot.vm_id).distinct()).all())
    validatable = [vm.id for vm in matched if vm.id in vm_ids_with_baselines]
    if not validatable:
        raise HTTPException(
            status_code=422,
            detail="No matched VM has a baseline; capture first.",
        )

    task = bulk_task_store.create(total=len(validatable))
    sched.last_fired_at = datetime.now(timezone.utc)
    sched.last_task_id = task.task_id
    db.commit()

    record_audit(
        db,
        action="validation_schedule.fired",
        actor=actor,
        resource_type="validation_schedule",
        resource_id=sched.id,
        details={
            "task_id": task.task_id,
            "vm_count": len(validatable),
            "trigger": "manual",
        },
    )
    db.commit()
    background_tasks.add_task(
        run_bulk_validation,
        task.task_id,
        vm_ids=validatable,
        actor=actor,
        use_cache=True,
    )
    request.state.skip_audit_log = True
    return {"task_id": task.task_id, "total": len(validatable)}
