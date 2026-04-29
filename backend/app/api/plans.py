from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.baseline import synthesize_profile
from app.core.db import get_db
from app.core.planner import MigrationPlanner, PlannerError
from app.core.reporter import ReporterError, WaveReporter, render_pdf
from app.models.plan import MigrationPlan
from app.models.validation import ValidationResult
from app.models.vm import VM, BaselineSnapshot
from app.schemas.plan import PlanCreate, PlanRead
from app.schemas.report import WaveReport

router = APIRouter(prefix="/plans", tags=["plans"])


def _assemble_vm_profiles(db: Session, vm_ids: list[int]) -> list[dict]:
    unique_ids = list(dict.fromkeys(vm_ids))
    vms = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(unique_ids))).all()}
    missing = [vid for vid in unique_ids if vid not in vms]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown vm_ids: {missing}")

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


@router.get("", response_model=list[PlanRead])
def list_plans(
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[MigrationPlan]:
    stmt = select(MigrationPlan).order_by(MigrationPlan.created_at.desc()).limit(limit)
    return list(db.scalars(stmt).all())


@router.get("/{plan_id}", response_model=PlanRead)
def get_plan(plan_id: int, db: Session = Depends(get_db)) -> MigrationPlan:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return plan


def _latest_validations_for(db: Session, vm_ids: list[int]) -> dict[int, ValidationResult]:
    latest: dict[int, ValidationResult] = {}
    for vid in vm_ids:
        row = db.scalars(
            select(ValidationResult)
            .where(ValidationResult.vm_id == vid)
            .order_by(ValidationResult.validated_at.desc())
            .limit(1)
        ).first()
        if row is not None:
            latest[vid] = row
    return latest


def _build_wave_report(db: Session, plan_id: int, wave_number: int) -> dict:
    """Load the plan + wave + per-VM verdicts and call the LLM reporter.

    Centralized so both the JSON and PDF endpoints share the same loading
    rules (404 for missing plan/wave, 409 for missing validations, 502 for
    LLM failures).
    """
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")

    wave = next((w for w in plan.waves if w.get("wave_number") == wave_number), None)
    if wave is None:
        raise HTTPException(
            status_code=404,
            detail=f"Wave {wave_number} not found in plan {plan_id}",
        )

    vm_ids: list[int] = list(wave.get("vm_ids") or [])
    if not vm_ids:
        raise HTTPException(status_code=422, detail=f"Wave {wave_number} has no VMs")

    vms_by_id = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(vm_ids))).all()}
    latest = _latest_validations_for(db, vm_ids)
    missing = [vid for vid in vm_ids if vid not in latest]
    if missing:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Missing validation results for vm_ids {missing}; "
                "run validation before requesting a wave report"
            ),
        )

    validation_payload = [
        {
            "vm_id": vid,
            "vm_name": vms_by_id[vid].name if vid in vms_by_id else "",
            "status": latest[vid].status.value,
            "summary": latest[vid].summary,
            "findings": latest[vid].findings or [],
            "remediation": latest[vid].remediation or [],
        }
        for vid in vm_ids
    ]

    reporter = WaveReporter()
    try:
        return reporter.generate(wave, validation_payload)
    except ReporterError as e:
        raise HTTPException(status_code=502, detail=f"Reporter failed: {e}") from e


def _pdf_response(report: dict, plan_id: int, wave_number: int) -> Response:
    pdf_bytes = render_pdf(report)
    filename = f"wave-{wave_number}-plan-{plan_id}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{plan_id}/waves/{wave_number}/report")
def wave_report(
    plan_id: int,
    wave_number: int,
    format: str = Query(default="json", pattern="^(json|pdf)$"),
    db: Session = Depends(get_db),
):
    report = _build_wave_report(db, plan_id, wave_number)
    if format == "pdf":
        return _pdf_response(report, plan_id, wave_number)
    return WaveReport(**report)


@router.get(
    "/{plan_id}/waves/{wave_number}/report/pdf",
    responses={200: {"content": {"application/pdf": {}}}},
)
def wave_report_pdf(
    plan_id: int,
    wave_number: int,
    db: Session = Depends(get_db),
) -> Response:
    """Dedicated PDF endpoint — always returns Content-Type: application/pdf."""
    report = _build_wave_report(db, plan_id, wave_number)
    return _pdf_response(report, plan_id, wave_number)
