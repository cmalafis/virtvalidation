from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.baseline import synthesize_profile
from app.core.db import get_db
from app.models.vm import VM, BaselineSnapshot, VMStatus
from app.schemas.vm import (
    BaselineProfile,
    SnapshotCreate,
    SnapshotRead,
    VMCreate,
    VMRead,
    VMUpdate,
)

router = APIRouter(prefix="/vms", tags=["vms"])


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
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"VM with name '{payload.name}' already exists"
        )
    db.refresh(vm)
    return vm


@router.get("", response_model=list[VMRead])
def list_vms(
    db: Session = Depends(get_db),
    status_filter: VMStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[VM]:
    stmt = select(VM).order_by(VM.created_at.desc()).limit(limit).offset(offset)
    if status_filter is not None:
        stmt = stmt.where(VM.status == status_filter)
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}", response_model=VMRead)
def get_vm(vm_id: int, db: Session = Depends(get_db)) -> VM:
    return _get_vm_or_404(db, vm_id)


@router.patch("/{vm_id}", response_model=VMRead)
def update_vm(vm_id: int, payload: VMUpdate, db: Session = Depends(get_db)) -> VM:
    vm = _get_vm_or_404(db, vm_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(vm, field, value)
    db.commit()
    db.refresh(vm)
    return vm


@router.delete("/{vm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vm(vm_id: int, db: Session = Depends(get_db)) -> None:
    vm = _get_vm_or_404(db, vm_id)
    db.delete(vm)
    db.commit()


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
            select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0))
            .where(BaselineSnapshot.vm_id == vm.id)
        )
        + 1
    )
    snapshot = BaselineSnapshot(
        vm_id=vm.id, snapshot_number=next_number, **payload.model_dump()
    )
    db.add(snapshot)
    if vm.status == VMStatus.discovered:
        vm.status = VMStatus.baseline_captured
    db.commit()
    db.refresh(snapshot)
    return snapshot


@router.get("/{vm_id}/snapshots", response_model=list[SnapshotRead])
def list_snapshots(
    vm_id: int, db: Session = Depends(get_db)
) -> list[BaselineSnapshot]:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}/snapshots/{snapshot_id}", response_model=SnapshotRead)
def get_snapshot(
    vm_id: int, snapshot_id: int, db: Session = Depends(get_db)
) -> BaselineSnapshot:
    snapshot = db.get(BaselineSnapshot, snapshot_id)
    if snapshot is None or snapshot.vm_id != vm_id:
        raise HTTPException(
            status_code=404, detail=f"Snapshot {snapshot_id} not found for VM {vm_id}"
        )
    return snapshot


@router.get("/{vm_id}/baseline/history", response_model=list[SnapshotRead])
def baseline_history(
    vm_id: int, db: Session = Depends(get_db)
) -> list[BaselineSnapshot]:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}/baseline/profile", response_model=BaselineProfile)
def baseline_profile(
    vm_id: int, db: Session = Depends(get_db)
) -> BaselineProfile:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.asc())
    )
    snapshots = list(db.scalars(stmt).all())
    return synthesize_profile(vm_id, snapshots)
