import logging
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.config import settings as _app_settings
from app.core.db import get_db
from app.core.mapping_validation import validate_plan_inputs
from app.core.mtv import (
    MappingResolver,
    MTVGenerationError,
    WaveContext,
    generate_wave_yaml,
)
from app.core.plan_generation import run_simple_plan_generation
from app.core.preclassifier import PreClassifier
from app.core.reporter import ReporterError, WaveReporter, render_pdf
from app.core.vm_lifecycle import (
    LifecycleTransitionError,
    transition_to_available_from_deleted_plan,
    transition_to_migrated,
    transition_to_planned,
)
from app.models.plan import MigrationPlan, PlanningStrategy
from app.models.target import ResourceMapping
from app.models.validation import ValidationResult
from app.models.vm import VM
from app.schemas.plan import (
    PlanCreate,
    PlanningStrategyCreate,
    PlanningStrategyRead,
    PlanningStrategyUpdate,
    PlanRead,
    PreviewGroupsResponse,
)
from app.schemas.report import WaveReport

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plans"])


def _resolve_active_mapping_for_vms(db: Session, vms: list[VM]) -> ResourceMapping | None:
    """Auto-pick the active ResourceMapping when the operator didn't
    pass ``mapping_id``.

    Returns None when:
      - VMs span multiple ``source_vcenter_id`` values (ambiguous;
        operator must pick explicitly via the wizard)
      - no VM has a ``source_vcenter_id``
      - no active mapping exists for the single shared vCenter

    When more than one active mapping exists for the same
    (vcenter, ocp_target) pair — a data inconsistency the editor's
    PATCH normally prevents by flipping siblings off — log and pick
    the lowest id deterministically. Stage 0 validation will catch
    coverage gaps regardless.
    """
    vcenter_ids = {vm.source_vcenter_id for vm in vms if vm.source_vcenter_id is not None}
    if len(vcenter_ids) != 1:
        return None
    (vc_id,) = vcenter_ids
    candidates = list(
        db.scalars(
            select(ResourceMapping)
            .where(ResourceMapping.vcenter_source_id == vc_id)
            .where(ResourceMapping.is_active.is_(True))
            .order_by(ResourceMapping.id.asc())
        ).all()
    )
    if not candidates:
        return None
    if len(candidates) > 1:
        logger.warning(
            "plan.create.multiple_active_mappings vcenter_id=%d ids=%s picked=%d",
            vc_id,
            [m.id for m in candidates],
            candidates[0].id,
        )
    return candidates[0]


@router.post("", response_model=PlanRead, status_code=status.HTTP_202_ACCEPTED)
def create_plan(
    payload: PlanCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Kick off async migration plan generation.

    Returns 202 within ~1 second with the plan row in ``status=pending``.
    The actual work — preclassification, LLM rationale, wave assembly —
    runs in a FastAPI BackgroundTask that writes ``status`` /
    ``progress_message`` / ``progress_percent`` to the plan row as it
    advances. The frontend polls ``GET /api/plans/{id}`` every 2s and
    transitions the modal through the visible status states.

    On LLM failure the verbatim typed-exception message lands in
    ``Plan.error_message`` — operators see "Cannot reach KServe at
    http://wrong.endpoint", not a generic "Plan generation failed".

    Fail-fast pre-flight: missing vm_ids 404 immediately so the
    operator doesn't wait for the background task to surface the
    same answer.
    """
    unique_ids = list(dict.fromkeys(payload.vm_ids))
    if not unique_ids:
        raise HTTPException(status_code=422, detail="vm_ids must contain at least one VM")
    # Selection cap. Architectural rule, not just a soft hint — the
    # operator workflow is many small auditable plans, not one giant
    # black-box plan. See settings.max_vms_per_plan.
    max_vms = _app_settings.max_vms_per_plan
    if len(unique_ids) > max_vms:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Plan exceeds selection cap: {len(unique_ids)} VMs requested, "
                f"max {max_vms} per plan. Narrow filters or split into multiple plans."
            ),
        )
    vms = list(db.scalars(select(VM).where(VM.id.in_(unique_ids))).all())
    known_ids = {v.id for v in vms}
    missing = [vid for vid in unique_ids if vid not in known_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown vm_ids: {missing}")

    # Resolve mapping. The new wizard always sends ``mapping_id``; CLI
    # / script callers can omit it and we'll auto-resolve the active
    # mapping for the VMs' source vCenter. Stage 0 validation below
    # runs against whichever mapping we landed on (or None if neither
    # path produced one), so coverage gaps still surface synchronously.
    mapping: ResourceMapping | None = None
    if payload.mapping_id is not None:
        mapping = db.get(ResourceMapping, payload.mapping_id)
        if mapping is None:
            raise HTTPException(
                status_code=404,
                detail=f"Resource mapping {payload.mapping_id} not found",
            )
    else:
        mapping = _resolve_active_mapping_for_vms(db, vms)

    # Persist whichever mapping ID we actually consulted (auto-resolved
    # or operator-supplied) so the background task + audit trail show
    # the mapping that drove the plan.
    mapping_id_used = mapping.id if mapping is not None else None

    coverage = validate_plan_inputs(vms, mapping)
    if not coverage.ok:
        raise HTTPException(status_code=422, detail=coverage.render())

    plan = MigrationPlan(
        name=payload.name,
        vm_ids=unique_ids,
        waves=[],
        # ``model`` is non-null on the column; the planner overwrites it
        # when it completes. Empty string is the sentinel for "not yet
        # generated".
        model="",
        mapping_id=mapping_id_used,
        status="pending",
        progress_message="Queued",
        progress_percent=0,
        started_at=datetime.now(timezone.utc),
    )
    db.add(plan)
    db.flush()  # need plan.id for the lifecycle audit row

    # Lifecycle precondition: every selected VM must be ``available``.
    # The lifecycle service raises if any VM is already in a plan; that
    # surfaces as a 422 so the operator sees exactly which VMs blocked
    # the request.
    try:
        transition_to_planned(unique_ids, plan.id, db)
    except LifecycleTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    db.commit()
    db.refresh(plan)

    background_tasks.add_task(
        run_simple_plan_generation,
        plan.id,
        vm_ids=unique_ids,
        ha_strategy=payload.ha_strategy,
        preclassification_enabled=payload.preclassification_enabled,
    )

    body = PlanRead.model_validate(plan).model_dump(mode="json")
    # Legacy fields the dashboard's old success toast reads. They stay
    # empty until the background task completes; the frontend's poll
    # loop replaces the row with the completed version.
    body["groups"] = []
    body["groups_formed"] = 0
    body["method"] = ""
    body["attempts"] = 0
    return body


@router.post("/preview-groups", response_model=PreviewGroupsResponse)
def preview_groups(payload: PlanCreate, db: Session = Depends(get_db)) -> dict:
    """Show how the pre-classifier WOULD group these VMs — no LLM, no plan.

    The plan wizard calls this before submission so the operator sees
    the mechanical grouping (e.g. "57 VMs → 8 groups, web tier first,
    db tier last") without paying the LLM round-trip. Tells operators
    why specific VMs were clustered together, which is useful for
    debugging both unexpected groupings and missing metadata.
    """
    unique_ids = list(dict.fromkeys(payload.vm_ids))
    vms = list(db.scalars(select(VM).where(VM.id.in_(unique_ids))).all())
    missing = [vid for vid in unique_ids if vid not in {v.id for v in vms}]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown vm_ids: {missing}")

    classifier = PreClassifier()
    groups = classifier.classify(vms)
    # The preclassifier no longer caps group count — the per-LLM-call
    # ceiling is enforced at the wave-rationale stage instead. We
    # still surface the ceiling so the UI can show "this plan will
    # need N LLM calls" if it wants to.
    from app.core.wave_skeleton import MAX_VMS_PER_WAVE

    per_call_ceiling = MAX_VMS_PER_WAVE
    return {
        "vm_count": len(vms),
        "groups_formed": len(groups),
        "groups": [g.to_api_dict() for g in groups],
        "over_ceiling": False,  # Always False post-refactor (uncapped).
        "ceiling": per_call_ceiling,
    }


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


@router.delete("/{plan_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_plan(
    request: Request,
    plan_id: int,
    db: Session = Depends(get_db),
) -> None:
    """Delete a plan and return its VMs to ``available``.

    Permitted for plans in any status. The lifecycle service is
    idempotent on VMs already moved out of ``planned`` (operator-driven
    revert flows might have done so) — those rows are skipped silently
    rather than rolled back from ``migrated``.
    """
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")

    actor = request.headers.get("x-actor", "user")
    record_audit(
        db,
        action="plan.delete",
        actor=actor,
        resource_type="plan",
        resource_id=plan.id,
        details={
            "name": plan.name,
            "status": plan.status,
            "vm_count": len(plan.vm_ids or []),
        },
    )
    transition_to_available_from_deleted_plan(plan.id, list(plan.vm_ids or []), db, actor=actor)
    db.delete(plan)
    db.commit()
    request.state.skip_audit_log = True


@router.post("/{plan_id}/mark-succeeded", response_model=PlanRead)
def mark_plan_succeeded(
    request: Request,
    plan_id: int,
    db: Session = Depends(get_db),
) -> MigrationPlan:
    """Operator declares the cutover finished; VMs transition to ``migrated``.

    Plan must be in status ``complete`` (the new pipeline's terminal
    success state) — refusing on any other status prevents marking a
    half-generated plan as done. Idempotent on plans already in
    ``migrated`` (operator may double-click).
    """
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    if plan.status not in ("complete", "migrated"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Plan {plan_id} is in status {plan.status!r}; only plans in "
                "``complete`` may be marked succeeded."
            ),
        )

    actor = request.headers.get("x-actor", "user")
    try:
        transition_to_migrated(plan.id, list(plan.vm_ids or []), db, actor=actor)
    except LifecycleTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    plan.status = "migrated"
    record_audit(
        db,
        action="plan.mark_succeeded",
        actor=actor,
        resource_type="plan",
        resource_id=plan.id,
        details={
            "vm_count": len(plan.vm_ids or []),
            "wave_count": len(plan.waves or []),
        },
    )
    db.commit()
    db.refresh(plan)
    request.state.skip_audit_log = True
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


def _record_report_export(
    db: Session,
    plan_id: int,
    wave_number: int,
    actor: str,
) -> None:
    """Audit a PDF export. Called from GET endpoints (middleware skips reads)."""
    record_audit(
        db,
        action="report.export",
        actor=actor or "anonymous",
        resource_type="plan",
        resource_id=plan_id,
        details={"wave_number": wave_number, "format": "pdf"},
    )
    db.commit()


@router.get("/{plan_id}/waves/{wave_number}/report")
def wave_report(
    request: Request,
    plan_id: int,
    wave_number: int,
    format: str = Query(default="json", pattern="^(json|pdf)$"),
    db: Session = Depends(get_db),
):
    report = _build_wave_report(db, plan_id, wave_number)
    if format == "pdf":
        _record_report_export(db, plan_id, wave_number, request.headers.get("x-actor", "anonymous"))
        return _pdf_response(report, plan_id, wave_number)
    return WaveReport(**report)


@router.get(
    "/{plan_id}/waves/{wave_number}/report/pdf",
    responses={200: {"content": {"application/pdf": {}}}},
)
def wave_report_pdf(
    request: Request,
    plan_id: int,
    wave_number: int,
    db: Session = Depends(get_db),
) -> Response:
    """Dedicated PDF endpoint — always returns Content-Type: application/pdf."""
    report = _build_wave_report(db, plan_id, wave_number)
    _record_report_export(db, plan_id, wave_number, request.headers.get("x-actor", "anonymous"))
    return _pdf_response(report, plan_id, wave_number)


@router.get(
    "/{plan_id}/waves/{wave_number}/mtv-yaml",
    responses={200: {"content": {"application/yaml": {}}}},
)
def wave_mtv_yaml(
    request: Request,
    plan_id: int,
    wave_number: int,
    db: Session = Depends(get_db),
) -> Response:
    """Render the wave as a multi-doc MTV/Forklift YAML for ``oc apply -f``."""
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
    missing = [vid for vid in vm_ids if vid not in vms_by_id]
    if missing:
        raise HTTPException(status_code=409, detail=f"Unknown vm_ids in wave: {missing}")

    vm_payloads = [
        {
            "name": vms_by_id[vid].name,
            "vsphere_networks": list(vms_by_id[vid].vsphere_networks or []),
            "vsphere_datastores": list(vms_by_id[vid].vsphere_datastores or []),
            "environment": vms_by_id[vid].environment or "",
            "application_hint": vms_by_id[vid].application_hint or "",
            "target_namespace": vms_by_id[vid].target_namespace or "",
            "target_storage_class": vms_by_id[vid].target_storage_class or "",
            "target_network_attachment": vms_by_id[vid].target_network_attachment or "",
        }
        for vid in vm_ids
    ]

    resolver: MappingResolver | None = None
    used_mapping_id: int | None = None
    if plan.mapping_id is not None:
        mapping = db.get(ResourceMapping, plan.mapping_id)
        if mapping is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Plan {plan_id} references resource mapping "
                    f"{plan.mapping_id} but that mapping has been deleted. "
                    "Re-generate the plan with a valid mapping."
                ),
            )
        resolver = MappingResolver(
            network_mappings=list(mapping.network_mappings or []),
            storage_mappings=list(mapping.storage_mappings or []),
            namespace_mappings=list(mapping.namespace_mappings or []),
        )
        used_mapping_id = mapping.id
        # Stamp last_used_at so operators can spot stale mappings.
        from datetime import datetime, timezone

        mapping.last_used_at = datetime.now(timezone.utc)

    ctx = WaveContext.from_settings(
        plan_id=plan_id,
        wave_number=wave_number,
        rationale=wave.get("rationale", ""),
    )
    try:
        yaml_text = generate_wave_yaml(ctx, vm_payloads, resolver=resolver)
    except MTVGenerationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    record_audit(
        db,
        action="plan.export_mtv_yaml",
        actor=request.headers.get("x-actor", "anonymous"),
        resource_type="plan",
        resource_id=plan_id,
        details={"wave_number": wave_number, "mapping_id": used_mapping_id},
    )
    db.commit()

    filename = f"wave-{wave_number}-plan-{plan_id}.yaml"
    return Response(
        content=yaml_text,
        media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ===========================================================================
# Strategy-driven planning (PlanningStrategy CRUD + async generation +
# per-wave revisions). The legacy synchronous POST /api/plans path above
# stays for back-compat with existing tests; new wizard-driven flow goes
# through the endpoints below.
# ===========================================================================


# ---------------------------------------------------------------------------
# PlanningStrategy CRUD
# ---------------------------------------------------------------------------
strategies_router = APIRouter(tags=["planning-strategies"])


@strategies_router.get("", response_model=list[PlanningStrategyRead])
def list_strategies(db: Session = Depends(get_db)) -> list[PlanningStrategy]:
    return list(
        db.scalars(select(PlanningStrategy).order_by(PlanningStrategy.created_at.desc())).all()
    )


@strategies_router.post(
    "", response_model=PlanningStrategyRead, status_code=status.HTTP_201_CREATED
)
def create_strategy(
    request: Request,
    payload: PlanningStrategyCreate,
    db: Session = Depends(get_db),
) -> PlanningStrategy:
    actor = request.headers.get("x-actor", "user")
    strategy = PlanningStrategy(
        **payload.model_dump(),
        created_by_actor=actor,
    )
    db.add(strategy)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Strategy named {payload.name!r} already exists",
        ) from e
    db.refresh(strategy)

    record_audit(
        db,
        action="strategy.create",
        actor=actor,
        resource_type="strategy",
        resource_id=strategy.id,
        details={
            "name": strategy.name,
            "primary_grouping": strategy.primary_grouping.value,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return strategy


@strategies_router.get("/{strategy_id}", response_model=PlanningStrategyRead)
def get_strategy(strategy_id: int, db: Session = Depends(get_db)) -> PlanningStrategy:
    strategy = db.get(PlanningStrategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    return strategy


@strategies_router.patch("/{strategy_id}", response_model=PlanningStrategyRead)
def update_strategy(
    request: Request,
    strategy_id: int,
    payload: PlanningStrategyUpdate,
    db: Session = Depends(get_db),
) -> PlanningStrategy:
    strategy = db.get(PlanningStrategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(strategy, field, value)
    db.commit()
    db.refresh(strategy)
    request.state.skip_audit_log = True
    return strategy


@strategies_router.delete("/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_strategy(
    request: Request,
    strategy_id: int,
    db: Session = Depends(get_db),
) -> None:
    strategy = db.get(PlanningStrategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")
    record_audit(
        db,
        action="strategy.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="strategy",
        resource_id=strategy.id,
        details={"name": strategy.name},
    )
    db.delete(strategy)
    db.commit()
    request.state.skip_audit_log = True
