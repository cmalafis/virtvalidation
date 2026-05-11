from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.baseline import synthesize_profile
from app.core.capture import run_capture_task, task_store
from app.core.db import get_db
from app.core.limits import MAX_PAGE_SIZE
from app.core.validation import run_validation_task
from app.core.validation import task_store as validation_task_store
from app.models.validation import ValidationResult
from app.models.vm import VM, BaselineSnapshot, VMStatus
from app.schemas.validation import (
    LatestValidationResponse,
    ValidationTaskRead,
)
from app.schemas.vm import (
    BaselineProfile,
    BulkVMCreate,
    BulkVMDelete,
    BulkVMDeleteResult,
    BulkVMResult,
    CaptureTaskRead,
    SnapshotCreate,
    SnapshotRead,
    VMCreate,
    VMRead,
    VMUpdate,
)

router = APIRouter(tags=["vms"])


def _get_vm_or_404(db: Session, vm_id: int) -> VM:
    vm = db.get(VM, vm_id)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} not found")
    return vm


@router.post("", response_model=VMRead, status_code=status.HTTP_201_CREATED)
def create_vm(payload: VMCreate, db: Session = Depends(get_db)) -> VM:
    vm = VM(**payload.model_dump())
    db.add(vm)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"VM with name '{payload.name}' already exists"
        ) from e
    db.refresh(vm)
    return vm


@router.post("/bulk", response_model=BulkVMResult)
def create_vms_bulk(payload: BulkVMCreate, db: Session = Depends(get_db)) -> dict:
    """Best-effort batch enrollment.

    Pre-loads existing VM names so duplicates don't trigger per-row
    IntegrityErrors. Returns the created rows and a list of names that were
    skipped (with reason). Within-batch duplicates are also caught.
    """
    existing_names: set[str] = set(db.scalars(select(VM.name)).all())
    seen_in_batch: set[str] = set()
    created: list[VM] = []
    skipped: list[dict] = []

    for entry in payload.vms:
        name = entry.name
        if name in existing_names:
            skipped.append({"name": name, "reason": "already enrolled"})
            continue
        if name in seen_in_batch:
            skipped.append({"name": name, "reason": "duplicate within batch"})
            continue
        seen_in_batch.add(name)
        vm = VM(**entry.model_dump())
        db.add(vm)
        created.append(vm)

    db.commit()
    for vm in created:
        db.refresh(vm)

    return {
        "total": len(payload.vms),
        "created": created,
        "skipped": skipped,
    }


@router.get("", response_model=list[VMRead])
def list_vms(
    db: Session = Depends(get_db),
    status_filter: VMStatus | None = Query(default=None, alias="status"),
    # See app.core.limits.MAX_PAGE_SIZE. Default 100 keeps first-render
    # fast; the ceiling lets power-users pull a whole inventory in one
    # round-trip when scripting.
    limit: int = Query(default=100, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(default=0, ge=0),
) -> list[VM]:
    stmt = select(VM).order_by(VM.created_at.desc()).limit(limit).offset(offset)
    if status_filter is not None:
        stmt = stmt.where(VM.status == status_filter)
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}", response_model=VMRead)
def get_vm(vm_id: int, db: Session = Depends(get_db)) -> VM:
    return _get_vm_or_404(db, vm_id)


def _diff_for_audit(before: dict, after: dict) -> dict:
    """Return only the keys that actually changed, with before/after values.

    Used so the audit row's `details` column carries the exact field-level
    delta rather than the whole payload — matters for federal compliance
    review, where reviewers want to see what an operator actually changed.
    """
    delta: dict[str, dict] = {}
    for key in after:
        if before.get(key) != after.get(key):
            delta[key] = {"before": before.get(key), "after": after.get(key)}
    return delta


@router.patch("/{vm_id}", response_model=VMRead)
def update_vm(
    request: Request,
    vm_id: int,
    payload: VMUpdate,
    db: Session = Depends(get_db),
) -> VM:
    vm = _get_vm_or_404(db, vm_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        # Nothing to change — suppress the middleware audit so empty PATCHes
        # don't pollute the trail.
        request.state.skip_audit_log = True
        return vm

    before = {k: getattr(vm, k) for k in updates}
    for field, value in updates.items():
        setattr(vm, field, value)
    db.flush()
    after = {k: getattr(vm, k) for k in updates}

    record_audit(
        db,
        action="vm.update",
        actor=request.headers.get("x-actor", "user"),
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "diff": _diff_for_audit(before, after),
        },
    )
    db.commit()
    db.refresh(vm)
    request.state.skip_audit_log = True
    return vm


@router.delete("/{vm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vm(
    request: Request,
    vm_id: int,
    db: Session = Depends(get_db),
) -> None:
    vm = _get_vm_or_404(db, vm_id)
    # Audit FIRST so the trail records what was deleted; the audit_logs
    # table is intentionally not FK'd to vms, so the row survives the
    # cascade. Federal review can still answer "who deleted db-prod-01?"
    # months after the row is gone.
    record_audit(
        db,
        action="vm.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "source_hostname": vm.source_hostname,
            "ip_address": vm.ip_address,
        },
    )
    db.delete(vm)  # cascades snapshots + validations via the ORM relationships
    db.commit()
    request.state.skip_audit_log = True


@router.delete("", response_model=BulkVMDeleteResult)
def delete_vms_bulk(
    request: Request,
    payload: BulkVMDelete,
    db: Session = Depends(get_db),
) -> dict:
    """Delete multiple VMs in one round-trip.

    Returns the lists of deleted vs not-found ids. Each successfully
    deleted VM gets its own ``vm.delete`` audit row so per-VM forensics
    still work after a bulk operation.
    """
    actor = request.headers.get("x-actor", "user")
    requested = list(dict.fromkeys(payload.vm_ids))  # de-dupe, preserve order
    found_vms = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(requested))).all()}

    deleted: list[int] = []
    not_found: list[int] = []
    for vid in requested:
        vm = found_vms.get(vid)
        if vm is None:
            not_found.append(vid)
            continue
        record_audit(
            db,
            action="vm.delete",
            actor=actor,
            resource_type="vm",
            resource_id=vm.id,
            details={
                "vm_name": vm.name,
                "source_hostname": vm.source_hostname,
                "ip_address": vm.ip_address,
                "via_bulk": True,
            },
        )
        db.delete(vm)
        deleted.append(vid)
    db.commit()

    request.state.skip_audit_log = True
    return {"requested": len(requested), "deleted": deleted, "not_found": not_found}


# ---------------------------------------------------------------------------
# On-demand baseline capture
# ---------------------------------------------------------------------------
@router.post(
    "/{vm_id}/capture",
    response_model=CaptureTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_capture(
    request: Request,
    vm_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn an immediate baseline collection in the background.

    Returns 202 with the task handle immediately; the actual SSH session
    runs after the response is sent so the dashboard can poll for status.
    """
    vm = _get_vm_or_404(db, vm_id)
    actor = request.headers.get("x-actor", "user")

    task = task_store.create(vm_id=vm.id)
    record_audit(
        db,
        action="capture.triggered",
        actor=actor,
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "task_id": task.task_id,
            "via": "single",
        },
    )
    db.commit()

    background_tasks.add_task(run_capture_task, task.task_id, vm.id, actor=actor)
    request.state.skip_audit_log = True
    return task.to_dict()


@router.get("/{vm_id}/capture/{task_id}", response_model=CaptureTaskRead)
def get_capture_status(vm_id: int, task_id: str) -> dict:
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Capture task {task_id} not found. Tasks are kept in memory only "
                "and may have been cleared by an appliance restart — re-trigger "
                "the capture if needed."
            ),
        )
    if task.vm_id != vm_id:
        raise HTTPException(
            status_code=404,
            detail=f"Task {task_id} belongs to vm_id={task.vm_id}, not {vm_id}",
        )
    return task.to_dict()


@router.post(
    "/{vm_id}/snapshots",
    response_model=SnapshotRead,
    status_code=status.HTTP_201_CREATED,
)
def create_snapshot(
    vm_id: int, payload: SnapshotCreate, db: Session = Depends(get_db)
) -> BaselineSnapshot:
    vm = _get_vm_or_404(db, vm_id)
    next_number = (
        db.scalar(
            select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0)).where(
                BaselineSnapshot.vm_id == vm.id
            )
        )
        or 0
    ) + 1
    snapshot = BaselineSnapshot(vm_id=vm.id, snapshot_number=next_number, **payload.model_dump())
    db.add(snapshot)
    if vm.status == VMStatus.discovered:
        vm.status = VMStatus.baseline_captured
    db.commit()
    db.refresh(snapshot)
    return snapshot


@router.get("/{vm_id}/snapshots", response_model=list[SnapshotRead])
def list_snapshots(vm_id: int, db: Session = Depends(get_db)) -> list[BaselineSnapshot]:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}/snapshots/{snapshot_id}", response_model=SnapshotRead)
def get_snapshot(vm_id: int, snapshot_id: int, db: Session = Depends(get_db)) -> BaselineSnapshot:
    snapshot = db.get(BaselineSnapshot, snapshot_id)
    if snapshot is None or snapshot.vm_id != vm_id:
        raise HTTPException(
            status_code=404, detail=f"Snapshot {snapshot_id} not found for VM {vm_id}"
        )
    return snapshot


@router.get("/{vm_id}/validation/latest", response_model=LatestValidationResponse)
def latest_validation(vm_id: int, db: Session = Depends(get_db)) -> dict:
    """Return the latest validation result for a VM.

    "No validation yet" is a 200 with ``{"validation": null}`` — the VM
    detail page reads this on every load, and a 404 would force the UI
    to differentiate "VM doesn't exist" (real error) from "validation
    hasn't been run yet" (empty state) at the response level.
    """
    _get_vm_or_404(db, vm_id)
    row = db.scalars(
        select(ValidationResult)
        .where(ValidationResult.vm_id == vm_id)
        .order_by(ValidationResult.validated_at.desc())
        .limit(1)
    ).first()
    return {"validation": row}


# ---------------------------------------------------------------------------
# On-demand validation
# ---------------------------------------------------------------------------
@router.post(
    "/{vm_id}/validate",
    response_model=ValidationTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_validation(
    request: Request,
    vm_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn an immediate post-migration validation in the background.

    Returns 202 with the task handle. The actual SSH+LLM round-trip runs
    after the response is sent so the UI can poll for progress. A VM with
    no baseline is rejected up-front (400) — running the diff engine
    against an empty baseline produces a meaningless verdict.
    """
    vm = _get_vm_or_404(db, vm_id)
    actor = request.headers.get("x-actor", "user")

    has_baseline = (
        db.scalar(select(BaselineSnapshot.id).where(BaselineSnapshot.vm_id == vm.id).limit(1))
        is not None
    )
    if not has_baseline:
        request.state.skip_audit_log = True
        raise HTTPException(
            status_code=400,
            detail="Capture a baseline first before running validation",
        )

    task = validation_task_store.create(vm_id=vm.id)
    record_audit(
        db,
        action="validation.triggered",
        actor=actor,
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "task_id": task.task_id,
            "via": "single",
        },
    )
    db.commit()

    background_tasks.add_task(run_validation_task, task.task_id, vm.id, actor=actor)
    request.state.skip_audit_log = True
    return task.to_dict()


@router.get("/{vm_id}/validate/{task_id}", response_model=ValidationTaskRead)
def get_validation_status(vm_id: int, task_id: str) -> dict:
    task = validation_task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Validation task {task_id} not found. Tasks are kept in memory "
                "only and may have been cleared by an appliance restart — "
                "re-trigger the validation if needed."
            ),
        )
    if task.vm_id != vm_id:
        raise HTTPException(
            status_code=404,
            detail=f"Task {task_id} belongs to vm_id={task.vm_id}, not {vm_id}",
        )
    return task.to_dict()


@router.get("/{vm_id}/baseline/history", response_model=list[SnapshotRead])
def baseline_history(vm_id: int, db: Session = Depends(get_db)) -> list[BaselineSnapshot]:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}/baseline/profile", response_model=BaselineProfile)
def baseline_profile(vm_id: int, db: Session = Depends(get_db)) -> BaselineProfile:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.asc())
    )
    snapshots = list(db.scalars(stmt).all())
    return synthesize_profile(vm_id, snapshots)
