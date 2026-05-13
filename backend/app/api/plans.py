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
from app.core.baseline import synthesize_profile
from app.core.chunker import chunk_vms
from app.core.db import get_db
from app.core.llm.factory import get_llm_backend
from app.core.mtv import (
    MappingResolver,
    MTVGenerationError,
    WaveContext,
    generate_wave_yaml,
)
from app.core.plan_generation import (
    PlanRevisionError,
    apply_move_vm,
    resolve_scope,
    run_plan_generation,
    run_simple_plan_generation,
)
from app.core.plan_generation import task_store as plan_task_store
from app.core.preclassifier import PreClassifier
from app.core.reporter import ReporterError, WaveReporter, render_pdf
from app.models.chunk import PlanChunk
from app.models.plan import MigrationPlan, PlanningStrategy
from app.models.target import ResourceMapping
from app.models.validation import ValidationResult
from app.models.vcenter import VCenterSource
from app.models.vm import VM, BaselineSnapshot
from app.schemas.plan import (
    PlanChunkRead,
    PlanCreate,
    PlanGenerateRequest,
    PlanGenerationTaskRead,
    PlanningStrategyCreate,
    PlanningStrategyRead,
    PlanningStrategyUpdate,
    PlanRead,
    PreviewGroupsResponse,
    WaveMoveVMRequest,
)
from app.schemas.report import WaveReport

router = APIRouter(tags=["plans"])


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
                "vsphere_networks": list(vm.vsphere_networks or []),
                "vsphere_datastores": list(vm.vsphere_datastores or []),
                "target_namespace": vm.target_namespace or "",
                "target_storage_class": vm.target_storage_class or "",
                "target_network_attachment": vm.target_network_attachment or "",
                "baseline": profile.model_dump(mode="json"),
            }
        )
    return profiles


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
    known = {vid for vid in db.scalars(select(VM.id).where(VM.id.in_(unique_ids))).all()}
    missing = [vid for vid in unique_ids if vid not in known]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown vm_ids: {missing}")

    plan = MigrationPlan(
        vm_ids=unique_ids,
        waves=[],
        # ``model`` is non-null on the column; the planner overwrites it
        # when it completes. Empty string is the sentinel for "not yet
        # generated".
        model="",
        status="pending",
        progress_message="Queued",
        progress_percent=0,
        started_at=datetime.now(timezone.utc),
    )
    db.add(plan)
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


@router.get("/{plan_id}/chunks", response_model=list[PlanChunkRead])
def get_plan_chunks(plan_id: int, db: Session = Depends(get_db)) -> list[PlanChunk]:
    """Return the chunk breakdown for a hierarchically-planned plan.

    Empty list for single-shot plans (the orchestrator persists no
    chunks for inputs under :data:`SINGLE_SHOT_THRESHOLD`).
    """
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return list(
        db.scalars(
            select(PlanChunk).where(PlanChunk.plan_id == plan_id).order_by(PlanChunk.sequence_index)
        ).all()
    )


@router.post("/preview-chunks")
def preview_chunks(payload: PlanGenerateRequest, db: Session = Depends(get_db)) -> dict:
    """Return what the chunker WOULD produce for a given scope without
    invoking the LLM. The wizard calls this before submission so the
    operator sees the planned chunk breakdown ("8 chunks, avg 12 VMs,
    ~5-10 minutes") and can adjust scope if needed."""
    matched = resolve_scope(db, payload.scope.model_dump())
    if not matched:
        return {
            "vm_count": 0,
            "chunk_count": 0,
            "single_shot": False,
            "max_chunk_size": 0,
            "chunks": [],
            "warnings": ["Scope filter matched zero VMs"],
        }

    backend = get_llm_backend()
    vc_rows = list(db.scalars(select(VCenterSource)).all())
    classification_by_vc = {row.id: row.classification_level.value for row in vc_rows}

    # Inline the strategy if present so the preview matches what the
    # orchestrator will see.
    if payload.strategy_id is not None:
        strategy = db.get(PlanningStrategy, payload.strategy_id)
        if strategy is None:
            raise HTTPException(status_code=404, detail=f"Strategy {payload.strategy_id} not found")
    elif payload.inline_strategy is not None:
        strategy = PlanningStrategy(**payload.inline_strategy.model_dump())
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide either strategy_id or inline_strategy",
        )

    mapping = (
        db.get(ResourceMapping, payload.mapping_id) if payload.mapping_id is not None else None
    )

    from app.core.chunker import SINGLE_SHOT_THRESHOLD

    if len(matched) < SINGLE_SHOT_THRESHOLD:
        return {
            "vm_count": len(matched),
            "chunk_count": 1,
            "single_shot": True,
            "max_chunk_size": backend.max_planning_chunk_size,
            "chunks": [],
            "warnings": [
                f"Scope is below the single-shot threshold "
                f"({SINGLE_SHOT_THRESHOLD} VMs). Plan will use one LLM call "
                "instead of the chunked pipeline."
            ],
        }

    chunks = chunk_vms(
        matched,
        mappings=mapping,
        strategy=strategy,
        max_size=backend.max_planning_chunk_size,
        classification_by_vcenter=classification_by_vc,
    )
    warnings: list[str] = []
    oversized_apps: list[str] = []
    for c in chunks:
        # Detect a chunk that hit the cap because of an over-large
        # application. Surface the application name so the operator
        # knows which one will be subdivided.
        if c.size == backend.max_planning_chunk_size and (
            c.sub_key.get("application_hint") and c.sub_key["application_hint"] != "_unspecified_"
        ):
            app = c.sub_key["application_hint"]
            if app not in oversized_apps:
                oversized_apps.append(app)
    if oversized_apps:
        warnings.append(
            f"Backend max is {backend.max_planning_chunk_size} VMs per chunk; "
            f"these applications will be subdivided: {', '.join(oversized_apps)}."
        )

    return {
        "vm_count": len(matched),
        "chunk_count": len(chunks),
        "single_shot": False,
        "max_chunk_size": backend.max_planning_chunk_size,
        "chunks": [
            {
                "chunk_id": c.chunk_id,
                "label": c.sub_key.get("label") or "unlabeled",
                "size": c.size,
                "reason_for_chunk": c.reason_for_chunk,
                "is_foundation": bool(c.sub_key.get("is_foundation")),
            }
            for c in chunks
        ],
        "warnings": warnings,
    }


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


# ---------------------------------------------------------------------------
# Async plan generation
# ---------------------------------------------------------------------------
@router.post(
    "/generate",
    response_model=PlanGenerationTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_plan_generation(
    request: Request,
    payload: PlanGenerateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn strategy-driven plan generation as a BackgroundTask.

    The wizard either references a saved strategy (``strategy_id``)
    or embeds an inline one (``inline_strategy``) for one-off
    generation. Returns 202 with the task handle; poll
    ``GET /api/plans/generate/{task_id}/status`` for progress.
    """
    actor = request.headers.get("x-actor", "user")

    if payload.strategy_id is not None:
        strategy = db.get(PlanningStrategy, payload.strategy_id)
        if strategy is None:
            raise HTTPException(
                status_code=404,
                detail=f"Strategy {payload.strategy_id} not found",
            )
    elif payload.inline_strategy is not None:
        strategy = PlanningStrategy(
            **payload.inline_strategy.model_dump(),
            created_by_actor=actor,
        )
        db.add(strategy)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Strategy named {payload.inline_strategy.name!r} "
                    "already exists — reference it via strategy_id instead"
                ),
            ) from e
        db.refresh(strategy)
    else:
        raise HTTPException(
            status_code=422,
            detail="Provide either strategy_id or inline_strategy",
        )

    # Pre-flight mapping check — fail-fast on a bogus mapping_id and on
    # source/scope mismatches so the operator finds out at trigger time
    # rather than after the LLM call.
    mapping_id = payload.mapping_id
    if mapping_id is not None:
        mapping = db.get(ResourceMapping, mapping_id)
        if mapping is None:
            raise HTTPException(
                status_code=404,
                detail=f"Resource mapping {mapping_id} not found",
            )
        scope_vcenter_id = payload.scope.source_vcenter_id
        if scope_vcenter_id is not None and mapping.vcenter_source_id != scope_vcenter_id:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Mapping {mapping_id} is for vCenter source "
                    f"{mapping.vcenter_source_id}, but scope filter targets "
                    f"vCenter {scope_vcenter_id}"
                ),
            )

    # Pre-flight scope check — fail fast if the scope filter would
    # match zero VMs. Saves the operator a 30-second wait to find out
    # they typo'd the environment filter.
    scope_dict = payload.scope.model_dump()
    matched = resolve_scope(db, scope_dict)
    if not matched:
        raise HTTPException(
            status_code=422,
            detail="Scope filter matched zero VMs — adjust your selection",
        )

    task = plan_task_store.create()
    record_audit(
        db,
        action="plan.generation_triggered",
        actor=actor,
        resource_type="strategy",
        resource_id=strategy.id,
        details={
            "task_id": task.task_id,
            "plan_name": payload.name,
            "scope": scope_dict,
            "vm_count": len(matched),
            "mapping_id": mapping_id,
        },
    )
    db.commit()

    background_tasks.add_task(
        run_plan_generation,
        task.task_id,
        plan_name=payload.name,
        strategy_id=strategy.id,
        scope=scope_dict,
        actor=actor,
        mapping_id=mapping_id,
    )
    request.state.skip_audit_log = True
    return task.to_dict()


@router.get(
    "/generate/{task_id}/status",
    response_model=PlanGenerationTaskRead,
)
def get_plan_generation_status(task_id: str) -> dict:
    task = plan_task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Plan generation task {task_id} not found. Tasks are kept "
                "in memory only and may have been cleared by an appliance "
                "restart — re-trigger if needed."
            ),
        )
    return task.to_dict()


# ---------------------------------------------------------------------------
# Per-wave revision (move-vm)
# ---------------------------------------------------------------------------
@router.post("/{plan_id}/waves/{wave_number}/move-vm", response_model=PlanRead)
def move_vm_between_waves(
    request: Request,
    plan_id: int,
    wave_number: int,
    payload: WaveMoveVMRequest,
    db: Session = Depends(get_db),
) -> MigrationPlan:
    """Move one VM into a different wave, creating a new plan revision.

    The original plan is preserved; the response is the **new** plan
    (revision_number incremented, supersedes_plan_id pointing at the
    previous revision). The ``wave_number`` in the URL is informational
    — the new plan's wave membership is what counts.
    """
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    if payload.target_wave_number == wave_number:
        # No-op — the URL's wave_number is the source. Nothing to do.
        raise HTTPException(
            status_code=422,
            detail="target_wave_number equals the source wave_number",
        )
    try:
        revision = apply_move_vm(
            db,
            plan,
            vm_id=payload.vm_id,
            target_wave_number=payload.target_wave_number,
            actor=request.headers.get("x-actor", "user"),
            note=payload.note,
        )
    except PlanRevisionError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    request.state.skip_audit_log = True
    return revision
