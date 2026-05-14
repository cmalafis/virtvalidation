"""Wave-scoped baseline + validation endpoints.

Three routers in one file because the surface is small and tightly
coupled:

  - ``waves_router`` (mounted at ``/api/plans``) — wave-scoped actions:
    POST baseline, POST validate, POST revoke-validation-key.
  - ``baseline_runs_router`` (mounted at ``/api/baseline-runs``) — GET
    status + per-VM, POST retry-failed.
  - ``validation_runs_router`` (mounted at ``/api/validation-runs``) —
    GET status + per-VM verdicts.

Async pattern: each kick-off returns 202 with the run id; the UI polls
the GET endpoint. Background tasks live in
:mod:`app.core.collection.wave_jobs`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    status,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.collection.wave_jobs import (
    resolve_wave_vm_ids,
    run_baseline_task,
    run_validation_task,
)
from app.core.db import get_db
from app.models.baseline_run import (
    Baseline,
    BaselineRun,
    BaselineRunStatus,
    VMCollectionStatus,
)
from app.models.plan import MigrationPlan
from app.models.ssh_key import SSHKey, SSHKeyStatus
from app.models.validation_run import (
    ValidationRun,
    ValidationRunStatus,
    VMValidation,
    VMValidationVerdict,
)
from app.models.vm import VM
from app.schemas.baseline_run import (
    BaselineRead,
    BaselineRunAccepted,
    BaselineRunCreate,
    BaselineRunRead,
)
from app.schemas.ssh_key import (
    SSHKeyRevocationOutcome,
    SSHKeyRevokeResponse,
)
from app.schemas.validation_run import (
    RevokeValidationKeyRequest,
    ValidationRunAccepted,
    ValidationRunCreate,
    ValidationRunRead,
    VMValidationRead,
)
from app.services import ssh_key_service

logger = logging.getLogger(__name__)

waves_router = APIRouter(tags=["waves"])
baseline_runs_router = APIRouter(tags=["baseline-runs"])
validation_runs_router = APIRouter(tags=["validation-runs"])


def _actor(request: Request) -> str:
    return request.headers.get("x-actor", "user") if request else "user"


def _get_plan_or_404(db: Session, plan_id: int) -> MigrationPlan:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return plan


def _resolve_active_key(db: Session, ssh_key_id: int) -> SSHKey:
    key = db.get(SSHKey, ssh_key_id)
    if key is None:
        raise HTTPException(status_code=404, detail=f"SSH key {ssh_key_id} not found")
    if key.status != SSHKeyStatus.active:
        raise HTTPException(
            status_code=422,
            detail=f"SSH key {ssh_key_id} is retired; pick an active key",
        )
    return key


def _resolve_wave_vms(db: Session, plan: MigrationPlan, wave_number: int) -> list[VM]:
    """Resolve wave_number → list of VM rows. 422 with a helpful message
    when the wave is missing or empty (operator error)."""
    try:
        vm_ids = resolve_wave_vm_ids(plan, wave_number)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not vm_ids:
        raise HTTPException(
            status_code=422,
            detail=f"Wave {wave_number} of plan {plan.id} has no VMs",
        )
    vms = list(db.scalars(select(VM).where(VM.id.in_(vm_ids))).all())
    found = {vm.id for vm in vms}
    missing = sorted(set(vm_ids) - found)
    if missing:
        # VMs were deleted out from under the plan. Surface so the operator
        # decides whether to regenerate the plan or proceed with what's
        # still there.
        raise HTTPException(
            status_code=422,
            detail=(
                f"Wave {wave_number} references VMs that no longer exist: "
                f"{missing}. Regenerate the plan or remove the missing VMs."
            ),
        )
    return vms


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Baseline kick-off
# ---------------------------------------------------------------------------
@waves_router.post(
    "/{plan_id}/waves/{wave_number}/baseline",
    response_model=BaselineRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def kick_off_baseline_run(
    plan_id: int,
    wave_number: int,
    payload: BaselineRunCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> BaselineRunAccepted:
    plan = _get_plan_or_404(db, plan_id)
    key = _resolve_active_key(db, payload.ssh_key_id)
    vms = _resolve_wave_vms(db, plan, wave_number)

    run = BaselineRun(
        plan_id=plan_id,
        wave_number=wave_number,
        ssh_key_id=key.id,
        status=BaselineRunStatus.pending,
        total_vms=len(vms),
        captured_vms=0,
        failed_vms=0,
        progress_message="Queued",
    )
    db.add(run)
    db.flush()  # need run.id before creating children

    for vm in vms:
        db.add(
            Baseline(
                baseline_run_id=run.id,
                vm_id=vm.id,
                status=VMCollectionStatus.pending,
            )
        )

    record_audit(
        db,
        action="baseline_run.created",
        actor=_actor(request),
        resource_type="baseline_run",
        resource_id=run.id,
        details={
            "plan_id": plan_id,
            "wave_number": wave_number,
            "ssh_key_id": key.id,
            "total_vms": len(vms),
        },
    )
    db.commit()
    db.refresh(run)

    background_tasks.add_task(run_baseline_task, run.id)

    return BaselineRunAccepted(
        baseline_run_id=run.id,
        status=run.status,
        status_url=f"/api/baseline-runs/{run.id}",
    )


# ---------------------------------------------------------------------------
# Baseline run read + retry
# ---------------------------------------------------------------------------
@baseline_runs_router.get("/{run_id}", response_model=BaselineRunRead)
def get_baseline_run(run_id: int, db: Session = Depends(get_db)) -> BaselineRunRead:
    run = db.get(BaselineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Baseline run {run_id} not found")
    rows = list(
        db.scalars(
            select(Baseline)
            .where(Baseline.baseline_run_id == run_id)
            .order_by(Baseline.id.asc())
        ).all()
    )
    return BaselineRunRead(
        id=run.id,
        plan_id=run.plan_id,
        wave_number=run.wave_number,
        ssh_key_id=run.ssh_key_id,
        status=run.status,
        total_vms=run.total_vms,
        captured_vms=run.captured_vms,
        failed_vms=run.failed_vms,
        progress_message=run.progress_message,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        baselines=[BaselineRead.model_validate(r, from_attributes=True) for r in rows],
    )


@baseline_runs_router.post(
    "/{run_id}/retry-failed",
    response_model=BaselineRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def retry_failed_baseline(
    run_id: int,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> BaselineRunAccepted:
    prior = db.get(BaselineRun, run_id)
    if prior is None:
        raise HTTPException(status_code=404, detail=f"Baseline run {run_id} not found")
    if prior.status not in (BaselineRunStatus.completed, BaselineRunStatus.failed):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Baseline run {run_id} is still {prior.status.value if hasattr(prior.status, 'value') else prior.status}; "
                "wait for completion before retrying"
            ),
        )
    failed_rows = list(
        db.scalars(
            select(Baseline).where(
                Baseline.baseline_run_id == run_id,
                Baseline.status == VMCollectionStatus.failed,
            )
        ).all()
    )
    if not failed_rows:
        raise HTTPException(
            status_code=422, detail="No failed VMs to retry in this run"
        )

    # Verify the original SSH key is still active. Operators who retired
    # the key would otherwise hit a 500 from the background task.
    key = db.get(SSHKey, prior.ssh_key_id)
    if key is None or key.status != SSHKeyStatus.active:
        raise HTTPException(
            status_code=422,
            detail=(
                "Original SSH key is no longer active. Generate a new run "
                "via the wave's baseline endpoint instead."
            ),
        )

    new_run = BaselineRun(
        plan_id=prior.plan_id,
        wave_number=prior.wave_number,
        ssh_key_id=prior.ssh_key_id,
        status=BaselineRunStatus.pending,
        total_vms=len(failed_rows),
        captured_vms=0,
        failed_vms=0,
        progress_message="Queued (retry-failed)",
    )
    db.add(new_run)
    db.flush()

    for row in failed_rows:
        db.add(
            Baseline(
                baseline_run_id=new_run.id,
                vm_id=row.vm_id,
                status=VMCollectionStatus.pending,
            )
        )

    record_audit(
        db,
        action="baseline_run.retry_failed",
        actor=_actor(request),
        resource_type="baseline_run",
        resource_id=new_run.id,
        details={
            "prior_run_id": prior.id,
            "total_vms": len(failed_rows),
        },
    )
    db.commit()
    db.refresh(new_run)

    background_tasks.add_task(run_baseline_task, new_run.id)

    return BaselineRunAccepted(
        baseline_run_id=new_run.id,
        status=new_run.status,
        status_url=f"/api/baseline-runs/{new_run.id}",
    )


# ---------------------------------------------------------------------------
# Validation kick-off
# ---------------------------------------------------------------------------
@waves_router.post(
    "/{plan_id}/waves/{wave_number}/validate",
    response_model=ValidationRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def kick_off_validation_run(
    plan_id: int,
    wave_number: int,
    payload: ValidationRunCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
) -> ValidationRunAccepted:
    plan = _get_plan_or_404(db, plan_id)
    key = _resolve_active_key(db, payload.ssh_key_id)
    vms = _resolve_wave_vms(db, plan, wave_number)

    # Hard requirement per the brief: validation requires a completed
    # baseline run for this wave. Without it there's nothing to diff.
    has_baseline = db.scalars(
        select(BaselineRun)
        .where(
            BaselineRun.plan_id == plan_id,
            BaselineRun.wave_number == wave_number,
            BaselineRun.status == BaselineRunStatus.completed,
        )
        .limit(1)
    ).first()
    if has_baseline is None:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Wave {wave_number} of plan {plan_id} has no completed "
                "baseline run — capture a baseline before validating."
            ),
        )

    run = ValidationRun(
        plan_id=plan_id,
        wave_number=wave_number,
        ssh_key_id=key.id,
        status=ValidationRunStatus.pending,
        total_vms=len(vms),
        progress_message="Queued",
    )
    db.add(run)
    db.flush()

    for vm in vms:
        db.add(
            VMValidation(
                validation_run_id=run.id,
                vm_id=vm.id,
                verdict=VMValidationVerdict.unreachable,
            )
        )

    record_audit(
        db,
        action="validation_run.created",
        actor=_actor(request),
        resource_type="validation_run",
        resource_id=run.id,
        details={
            "plan_id": plan_id,
            "wave_number": wave_number,
            "ssh_key_id": key.id,
            "total_vms": len(vms),
            "baseline_run_id": has_baseline.id,
        },
    )
    db.commit()
    db.refresh(run)

    background_tasks.add_task(run_validation_task, run.id)

    return ValidationRunAccepted(
        validation_run_id=run.id,
        status=run.status,
        status_url=f"/api/validation-runs/{run.id}",
    )


# ---------------------------------------------------------------------------
# Validation run read
# ---------------------------------------------------------------------------
@validation_runs_router.get("/{run_id}", response_model=ValidationRunRead)
def get_validation_run(run_id: int, db: Session = Depends(get_db)) -> ValidationRunRead:
    run = db.get(ValidationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Validation run {run_id} not found")
    rows = list(
        db.scalars(
            select(VMValidation)
            .where(VMValidation.validation_run_id == run_id)
            .order_by(VMValidation.id.asc())
        ).all()
    )
    return ValidationRunRead(
        id=run.id,
        plan_id=run.plan_id,
        wave_number=run.wave_number,
        ssh_key_id=run.ssh_key_id,
        status=run.status,
        total_vms=run.total_vms,
        passed_vms=run.passed_vms,
        warned_vms=run.warned_vms,
        failed_vms=run.failed_vms,
        unreachable_vms=run.unreachable_vms,
        progress_message=run.progress_message,
        started_at=run.started_at,
        completed_at=run.completed_at,
        created_at=run.created_at,
        validations=[VMValidationRead.model_validate(r, from_attributes=True) for r in rows],
    )


# ---------------------------------------------------------------------------
# Day-2: revoke validation key from a wave's VMs
# ---------------------------------------------------------------------------
@waves_router.post(
    "/{plan_id}/waves/{wave_number}/revoke-validation-key",
    response_model=SSHKeyRevokeResponse,
)
def revoke_validation_key(
    plan_id: int,
    wave_number: int,
    payload: RevokeValidationKeyRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> SSHKeyRevokeResponse:
    """Remove the key's public component from each VM in the wave's
    ``authorized_keys`` file. Gated on a clean validation run unless
    ``force=true``.
    """
    plan = _get_plan_or_404(db, plan_id)
    key = db.get(SSHKey, payload.ssh_key_id)
    if key is None:
        raise HTTPException(status_code=404, detail=f"SSH key {payload.ssh_key_id} not found")
    vms = _resolve_wave_vms(db, plan, wave_number)

    if not payload.force:
        # Require a completed validation run with no `fail` verdicts before
        # allowing the key cleanup. This is the brief's safety gate — the
        # operator shouldn't accidentally strand themselves out of a wave
        # they still need to remediate.
        latest = db.scalars(
            select(ValidationRun)
            .where(
                ValidationRun.plan_id == plan_id,
                ValidationRun.wave_number == wave_number,
                ValidationRun.status == ValidationRunStatus.completed,
            )
            .order_by(ValidationRun.id.desc())
        ).first()
        if latest is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Cannot revoke validation key: no completed validation "
                    "run for this wave. Validate first, or pass force=true."
                ),
            )
        if latest.failed_vms > 0:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Cannot revoke validation key: latest validation run "
                    f"has {latest.failed_vms} fail verdict(s). Resolve "
                    "or pass force=true to override."
                ),
            )

    targets: list[dict] = []
    synthetic: list[SSHKeyRevocationOutcome] = []
    for vm in vms:
        host = vm.ip_address or vm.target_hostname or vm.source_hostname
        if not host:
            synthetic.append(
                SSHKeyRevocationOutcome(
                    vm_id=vm.id, succeeded=False, detail="no_host_address"
                )
            )
            continue
        targets.append(
            {
                "vm_id": vm.id,
                "host": host,
                "port": vm.ssh_port or 22,
                "username": vm.ssh_user or "virtvalidate",
            }
        )

    real_outcomes = ssh_key_service.revoke_key_from_vms(
        db, key_id=payload.ssh_key_id, vm_targets=targets
    )
    outcomes = synthetic + [
        SSHKeyRevocationOutcome(vm_id=o.vm_id, succeeded=o.succeeded, detail=o.detail)
        for o in real_outcomes
    ]
    succeeded = sum(1 for o in outcomes if o.succeeded)
    failed = sum(1 for o in outcomes if not o.succeeded)

    record_audit(
        db,
        action="wave.revoke_validation_key",
        actor=_actor(request),
        resource_type="ssh_key",
        resource_id=payload.ssh_key_id,
        details={
            "plan_id": plan_id,
            "wave_number": wave_number,
            "total": len(outcomes),
            "succeeded": succeeded,
            "failed": failed,
            "force": payload.force,
        },
    )
    db.commit()
    return SSHKeyRevokeResponse(
        key_id=payload.ssh_key_id,
        total=len(outcomes),
        succeeded=succeeded,
        failed=failed,
        outcomes=outcomes,
    )
