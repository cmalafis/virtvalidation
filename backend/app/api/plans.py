from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.baseline import synthesize_profile
from app.core.db import get_db
from app.core.planner import MigrationPlanner, PlannerError
from app.models.plan import MigrationPlan
from app.models.vm import VM, BaselineSnapshot
from app.schemas.plan import PlanCreate, PlanRead

router = APIRouter(prefix="/plans", tags=["plans"])


def _assemble_vm_profiles(db: Session, vm_ids: list[int]) -> list[dict]:
    unique_ids = list(dict.fromkeys(vm_ids))
    vms = {
        vm.id: vm
        for vm in db.scalars(select(VM).where(VM.id.in_(unique_ids))).all()
    }
    missing = [vid for vid in unique_ids if vid not in vms]
    if missing:
        raise HTTPException(
            status_code=404, detail=f"Unknown vm_ids: {missing}"
        )

    profiles: list[dict] = []
    for vid in unique_ids:
        vm = vms[vid]
        snapshots = list(
            db.scalars(
                select(BaselineSnapshot)
                .where(BaselineSnapshot.vm_id == vid)
                .order_by(BaselineSnapshot.collected_at.asc())
            ).all()
        )
        profile = synthesize_profile(vid, snapshots)
        profiles.append(
            {
                "vm_id": vid,
                "name": vm.name,
                "role": vm.role or "",
                "os_family": vm.os_family or "",
                "baseline": profile.model_dump(mode="json"),
            }
        )
    return profiles


@router.post("", response_model=PlanRead, status_code=status.HTTP_201_CREATED)
def create_plan(payload: PlanCreate, db: Session = Depends(get_db)) -> MigrationPlan:
    profiles = _assemble_vm_profiles(db, payload.vm_ids)

    planner = MigrationPlanner()
    try:
        result = planner.plan(profiles)
    except PlannerError as e:
        raise HTTPException(status_code=502, detail=f"Planner failed: {e}") from e

    plan = MigrationPlan(
        vm_ids=[p["vm_id"] for p in profiles],
        waves=result["waves"],
        summary=result.get("summary") or None,
        model=planner.model,
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


@router.get("/{plan_id}", response_model=PlanRead)
def get_plan(plan_id: int, db: Session = Depends(get_db)) -> MigrationPlan:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return plan
