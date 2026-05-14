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
from app.core.target_resolution import resolve_vms_iter
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
    PlanPreviewResponse,
    PlanRead,
    PreviewGroupsResponse,
)
from app.schemas.report import WaveReport

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plans"])


def _auto_resolve_mappings_for_vms(db: Session, vms: list[VM]) -> list[ResourceMapping]:
    """Auto-pick the resolved ResourceMapping per VM in the selection.

    Used when the caller omitted ``mapping_ids`` entirely (CLI / pre-
    wizard scripts). Returns the deduplicated set of mappings reached
    via :func:`app.core.target_resolution.resolve_vms_iter` — each VM's
    ``(source_vcenter_id, resolved_cluster_id)`` resolves to at most
    one mapping under the per-pair uniqueness constraint. VMs whose
    resolution is incomplete contribute no mapping to the list; Stage 0
    then surfaces the gap synchronously.
    """
    resolved = resolve_vms_iter(vms, db)
    seen: dict[int, ResourceMapping] = {}
    for r in resolved.values():
        if r.mapping_id is None or r.mapping_id in seen:
            continue
        mapping = db.get(ResourceMapping, r.mapping_id)
        if mapping is not None:
            seen[mapping.id] = mapping
    return list(seen.values())


def _partition_vms_for_plans(vms: list[VM], db: Session) -> tuple[list[tuple], list[VM]]:
    """Partition selected VMs by (vcenter, cluster, namespace) using
    the target resolver. Returns ``(partitions, unresolvable_vms)``
    where ``partitions`` is a list of
    ``((vc_id, cluster_id, namespace), [VM])`` and ``unresolvable``
    is every VM whose resolution didn't yield a cluster + namespace.
    """
    resolutions = resolve_vms_iter(vms, db)
    by_key: dict[tuple, list[VM]] = {}
    unresolvable: list[VM] = []
    for vm in vms:
        r = resolutions.get(vm.id)
        if r is None or r.cluster_id is None or not r.namespace:
            unresolvable.append(vm)
            continue
        key = (vm.source_vcenter_id, r.cluster_id, r.namespace)
        by_key.setdefault(key, []).append(vm)
    return list(by_key.items()), unresolvable


def _slug_for_plan_name(value: str, max_len: int = 63) -> str:
    """RFC1123-safe lowercase slug. Falls back to ``plan`` when empty.

    Used to compose multi-partition plan names: the user's chosen base
    name + vcenter / cluster / namespace suffixes, joined with ``-``,
    each segment slugified independently then concatenated and
    truncated to max_len.
    """
    out: list[str] = []
    last_dash = False
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash and out:
            out.append("-")
            last_dash = True
    slug = "".join(out).strip("-")
    return (slug or "plan")[:max_len]


def _compose_plan_name(
    base: str, vcenter_name: str | None, cluster_name: str | None, namespace: str | None
) -> str:
    parts = [base]
    for s in (vcenter_name, cluster_name, namespace):
        if s:
            parts.append(s)
    return _slug_for_plan_name("-".join(parts))


@router.post(
    "",
    response_model=PlanRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_plan(
    payload: PlanCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Kick off async migration plan generation.

    Selections are fanned out by ``(source_vcenter_id,
    target_cluster_id, target_namespace)`` partition — each partition
    becomes one MigrationPlan row with its own background generation
    task and its own MTV CR set at YAML-export time. The response
    surfaces the first plan's PlanRead with a ``plans`` list of all
    fanned-out plans so callers see the full set.

    On LLM failure the verbatim typed-exception message lands in
    each plan's ``error_message``.
    """
    unique_ids = list(dict.fromkeys(payload.vm_ids))
    if not unique_ids:
        raise HTTPException(status_code=422, detail="vm_ids must contain at least one VM")
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

    # Partition by (vcenter, cluster, namespace). Unresolvable VMs
    # surface immediately as a 422 so the operator fixes the
    # mapping / override before the background tasks fire.
    partitions, unresolvable = _partition_vms_for_plans(vms, db)
    if unresolvable:
        names = [vm.name for vm in unresolvable[:10]]
        suffix = "" if len(unresolvable) <= 10 else f" (+{len(unresolvable) - 10} more)"
        raise HTTPException(
            status_code=422,
            detail=(
                f"{len(unresolvable)} VM(s) cannot be resolved to a target cluster + namespace: "
                f"{', '.join(names)}{suffix}. Set target_cluster_id_override / "
                "target_namespace_override on the affected VMs or add a ResourceMapping."
            ),
        )
    if not partitions:
        raise HTTPException(status_code=422, detail="No partitions resolved from selection.")

    # If the caller supplied an explicit mapping_ids list, use it
    # verbatim (CLI / scripts). Otherwise auto-resolve per partition.
    explicit_mapping_ids: list[int] | None = None
    if payload.mapping_ids is not None:
        if payload.mapping_ids:
            rows = list(
                db.scalars(
                    select(ResourceMapping).where(ResourceMapping.id.in_(payload.mapping_ids))
                ).all()
            )
            found_ids = {m.id for m in rows}
            missing = [mid for mid in payload.mapping_ids if mid not in found_ids]
            if missing:
                raise HTTPException(
                    status_code=404,
                    detail=f"Resource mapping(s) not found: {missing}",
                )
        explicit_mapping_ids = list(payload.mapping_ids)

    # Pre-build name metadata for slug composition. One DB hit each;
    # both tables are small.
    from app.models.target import OCPTarget
    from app.models.vcenter import VCenterSource

    vcenters = {v.id: v for v in db.scalars(select(VCenterSource)).all()}
    targets = {t.id: t for t in db.scalars(select(OCPTarget)).all()}

    # Build the plan rows in one pass so we can transition all VMs to
    # ``planned`` in a single atomic call afterward.
    created_plans: list[MigrationPlan] = []
    bg_specs: list[tuple] = []
    for (vc_id, cluster_id, namespace), p_vms in partitions:
        p_vm_ids = [vm.id for vm in p_vms]
        # Determine per-partition mapping ids: explicit list filtered
        # to mappings covering this (vcenter, cluster); else auto.
        if explicit_mapping_ids is not None:
            mapping_rows = [
                m
                for m in db.scalars(
                    select(ResourceMapping).where(
                        ResourceMapping.id.in_(explicit_mapping_ids),
                        ResourceMapping.vcenter_source_id == vc_id,
                        ResourceMapping.ocp_target_id == cluster_id,
                    )
                ).all()
            ]
        else:
            mapping_rows = list(
                db.scalars(
                    select(ResourceMapping).where(
                        ResourceMapping.vcenter_source_id == vc_id,
                        ResourceMapping.ocp_target_id == cluster_id,
                    )
                ).all()
            )

        # Stage 0 — pre-flight against this partition's mapping(s).
        coverage = validate_plan_inputs(p_vms, mapping_rows)
        if not coverage.ok:
            raise HTTPException(status_code=422, detail=coverage.render())

        # Compose a unique-per-partition plan name when fanning out.
        # Single-partition selections keep the user-supplied name
        # verbatim so the simple case looks unchanged.
        if len(partitions) == 1:
            plan_name = payload.name
        else:
            vc_name = vcenters.get(vc_id).name if vcenters.get(vc_id) else None
            cluster_name = targets.get(cluster_id).name if targets.get(cluster_id) else None
            plan_name = _compose_plan_name(payload.name, vc_name, cluster_name, namespace)

        plan = MigrationPlan(
            name=plan_name,
            vm_ids=p_vm_ids,
            waves=[],
            model="",
            mapping_ids=[m.id for m in mapping_rows],
            status="pending",
            progress_message="Queued",
            progress_percent=0,
            started_at=datetime.now(timezone.utc),
        )
        db.add(plan)
        db.flush()  # need plan.id before the lifecycle transition + bg task
        created_plans.append(plan)
        bg_specs.append((plan.id, p_vm_ids))

    # Atomic lifecycle transition for every selected VM. If any one
    # VM is already in another plan, the whole call fails and no
    # background tasks fire.
    try:
        transition_to_planned(unique_ids, created_plans[0].id, db)
    except LifecycleTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    db.commit()
    for plan in created_plans:
        db.refresh(plan)

    for plan_id, p_vm_ids in bg_specs:
        background_tasks.add_task(
            run_simple_plan_generation,
            plan_id,
            vm_ids=p_vm_ids,
            ha_strategy=payload.ha_strategy,
            preclassification_enabled=payload.preclassification_enabled,
        )

    # Response: the first plan's PlanRead at the top level (legacy
    # callers read id/status/etc directly) PLUS a ``plans`` list with
    # every fanned-out plan so the wizard can poll all of them.
    first = created_plans[0]
    body = PlanRead.model_validate(first).model_dump(mode="json")
    body["groups"] = []
    body["groups_formed"] = 0
    body["method"] = ""
    body["attempts"] = 0
    body["plans"] = [PlanRead.model_validate(p).model_dump(mode="json") for p in created_plans]
    body["plan_count"] = len(created_plans)
    return body


@router.post("/preview", response_model=PlanPreviewResponse)
def preview_plan_partitions(payload: PlanCreate, db: Session = Depends(get_db)) -> dict:
    """Show how the selection will fan out into Plan CRs without
    creating any plan rows. Each entry corresponds to one
    ``(source_vcenter, target_cluster, target_namespace)`` partition
    POST /api/plans would generate.

    Estimated waves is a ceiling: ``ceil(vm_count / max_vms_per_wave)``;
    actual wave count depends on the mechanical assigner's pack +
    HA-family split + concurrency analysis, which we don't run here
    to keep /preview cheap (no LLM calls, no DB writes).
    """
    from app.core.target_resolution import resolve_vms_iter as _resolve
    from app.core.wave_skeleton import MAX_VMS_PER_WAVE
    from app.models.target import OCPTarget
    from app.models.vcenter import VCenterSource

    unique_ids = list(dict.fromkeys(payload.vm_ids))
    if not unique_ids:
        raise HTTPException(status_code=422, detail="vm_ids must contain at least one VM")
    vms = list(db.scalars(select(VM).where(VM.id.in_(unique_ids))).all())
    known_ids = {v.id for v in vms}
    missing = [vid for vid in unique_ids if vid not in known_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown vm_ids: {missing}")

    resolutions = _resolve(vms, db)
    vcenters = {v.id: v for v in db.scalars(select(VCenterSource)).all()}
    targets = {t.id: t for t in db.scalars(select(OCPTarget)).all()}
    mappings = list(db.scalars(select(ResourceMapping)).all())
    mapping_by_pair = {(m.vcenter_source_id, m.ocp_target_id): m for m in mappings}

    by_key: dict[tuple, dict] = {}
    unresolved: list[dict] = []
    for vm in vms:
        r = resolutions.get(vm.id)
        if r is None or r.cluster_id is None or not r.namespace:
            unresolved.append(
                {
                    "vm_id": vm.id,
                    "vm_name": vm.name,
                    "reasons": list(r.reasons) if r is not None else ["resolution unavailable"],
                }
            )
            continue
        key = (vm.source_vcenter_id, r.cluster_id, r.namespace)
        entry = by_key.get(key)
        if entry is None:
            mapping = mapping_by_pair.get((vm.source_vcenter_id, r.cluster_id))
            entry = {
                "source_vcenter_id": vm.source_vcenter_id,
                "source_vcenter_name": (
                    vcenters[vm.source_vcenter_id].name
                    if vm.source_vcenter_id in vcenters
                    else None
                ),
                "target_cluster_id": r.cluster_id,
                "target_cluster_name": (
                    targets[r.cluster_id].name if r.cluster_id in targets else None
                ),
                "target_namespace": r.namespace,
                "vm_count": 0,
                "estimated_waves": 0,
                "mapping_id": mapping.id if mapping is not None else None,
                "mapping_name": mapping.name if mapping is not None else None,
                "network_targets": [],
                "storage_targets": [],
            }
            by_key[key] = entry
        entry["vm_count"] += 1
        for n in r.networks:
            if n.target_network_name and n.target_network_name not in entry["network_targets"]:
                entry["network_targets"].append(n.target_network_name)
        for s in r.storage:
            if (
                s.target_storage_class_name
                and s.target_storage_class_name not in entry["storage_targets"]
            ):
                entry["storage_targets"].append(s.target_storage_class_name)

    # Compute estimated_waves now that vm_count is finalized.
    for entry in by_key.values():
        entry["estimated_waves"] = (entry["vm_count"] + MAX_VMS_PER_WAVE - 1) // MAX_VMS_PER_WAVE

    return {
        "total_vms": len(vms),
        "resolvable": sum(g["vm_count"] for g in by_key.values()),
        "unresolvable": len(unresolved),
        "groups": list(by_key.values()),
        "unresolved": unresolved,
    }


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
            # target_namespace fed from the per-VM override so the
            # MappingResolver's fallback ("vm has no namespace strategy
            # match" path) lands on the operator's declared namespace.
            "target_namespace": vms_by_id[vid].target_namespace_override or "",
        }
        for vid in vm_ids
    ]

    # Pick the mapping whose vcenter covers this wave's VMs. Per the
    # CLAUDE.md partition rule, every VM in a wave shares one
    # source_vcenter_id, so the lookup is unambiguous. Uses the plan's
    # mapping_ids snapshot taken at generation time.
    resolver: MappingResolver | None = None
    used_mapping_id: int | None = None
    wave_vcenter_ids = sorted(
        {
            vms_by_id[vid].source_vcenter_id
            for vid in vm_ids
            if vid in vms_by_id and vms_by_id[vid].source_vcenter_id is not None
        }
    )
    candidate_ids = list(plan.mapping_ids or [])

    mapping_for_wave: ResourceMapping | None = None
    if candidate_ids and wave_vcenter_ids:
        candidates = list(
            db.scalars(select(ResourceMapping).where(ResourceMapping.id.in_(candidate_ids))).all()
        )
        for m in candidates:
            if m.vcenter_source_id in wave_vcenter_ids:
                mapping_for_wave = m
                break
        if mapping_for_wave is None and len(candidates) < len(candidate_ids):
            missing = [mid for mid in candidate_ids if mid not in {c.id for c in candidates}]
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Plan {plan_id} references resource mapping(s) {missing} but "
                    "they have been deleted. Re-generate the plan with valid mappings."
                ),
            )

    if mapping_for_wave is not None:
        # ``namespace_mappings`` is a dict (NamespaceStrategy) or a
        # list (legacy criteria rows). DO NOT coerce to list — that
        # turns the dict into a list of its keys and the resolver
        # then iterates strings, raising AttributeError at request
        # time. The model's JSON column preserves whichever shape
        # the operator saved, and MappingResolver dispatches on it.
        resolver = MappingResolver(
            network_mappings=list(mapping_for_wave.network_mappings or []),
            storage_mappings=list(mapping_for_wave.storage_mappings or []),
            namespace_mappings=mapping_for_wave.namespace_mappings or [],
        )
        used_mapping_id = mapping_for_wave.id
        # Stamp last_used_at so operators can spot stale mappings.
        from datetime import datetime, timezone

        mapping_for_wave.last_used_at = datetime.now(timezone.utc)

    # Provider names: the source MTV Provider CR is by VCenterSource.name,
    # the destination Provider CR by OCPTarget.name. Both must already
    # exist on the cluster — VirtValidate emits Plan/NetworkMap/
    # StorageMap that reference them by name.
    source_provider_name = None
    destination_provider_name = None
    if mapping_for_wave is not None:
        from app.models.target import OCPTarget
        from app.models.vcenter import VCenterSource

        vc = db.get(VCenterSource, mapping_for_wave.vcenter_source_id)
        tgt = db.get(OCPTarget, mapping_for_wave.ocp_target_id)
        source_provider_name = vc.name if vc is not None else None
        destination_provider_name = tgt.name if tgt is not None else None

    ctx = WaveContext.from_settings(
        plan_id=plan_id,
        wave_number=wave_number,
        rationale=wave.get("rationale", ""),
        source_provider=source_provider_name,
        destination_provider=destination_provider_name,
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


@router.get("/{plan_id}/yaml", responses={200: {"content": {"application/zip": {}}}})
def plan_yaml_bundle(plan_id: int, db: Session = Depends(get_db)) -> Response:
    """Bundle every wave's MTV YAML for this plan into a single zip
    download. Each file is one wave's NetworkMap + StorageMap + Plan
    multi-doc YAML — same shape as the per-wave endpoint, just
    aggregated for one-click apply."""
    import io
    import zipfile

    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    if not plan.waves:
        raise HTTPException(status_code=422, detail=f"Plan {plan_id} has no waves yet")

    # Re-walk the per-wave generator so the bundle stays in lockstep
    # with the single-wave route (same resolver, same error
    # surfacing). We collect into an in-memory zip rather than
    # streaming because the YAML payload is small.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for wave in sorted(plan.waves, key=lambda w: w.get("wave_number", 0)):
            wave_number = wave.get("wave_number")
            if wave_number is None:
                continue
            vm_ids: list[int] = list(wave.get("vm_ids") or [])
            if not vm_ids:
                continue
            vms_by_id = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(vm_ids))).all()}
            vm_payloads = [
                {
                    "name": vms_by_id[vid].name,
                    "vsphere_networks": list(vms_by_id[vid].vsphere_networks or []),
                    "vsphere_datastores": list(vms_by_id[vid].vsphere_datastores or []),
                    "environment": vms_by_id[vid].environment or "",
                    "application_hint": vms_by_id[vid].application_hint or "",
                    "target_namespace": vms_by_id[vid].target_namespace_override or "",
                }
                for vid in vm_ids
                if vid in vms_by_id
            ]
            wave_vcenter_ids = sorted(
                {
                    vms_by_id[vid].source_vcenter_id
                    for vid in vm_ids
                    if vid in vms_by_id and vms_by_id[vid].source_vcenter_id is not None
                }
            )
            mapping_for_wave: ResourceMapping | None = None
            for m in db.scalars(
                select(ResourceMapping).where(ResourceMapping.id.in_(plan.mapping_ids or []))
            ).all():
                if m.vcenter_source_id in wave_vcenter_ids:
                    mapping_for_wave = m
                    break
            resolver = None
            source_name = None
            destination_name = None
            if mapping_for_wave is not None:
                resolver = MappingResolver(
                    network_mappings=list(mapping_for_wave.network_mappings or []),
                    storage_mappings=list(mapping_for_wave.storage_mappings or []),
                    namespace_mappings=mapping_for_wave.namespace_mappings or [],
                )
                from app.models.target import OCPTarget
                from app.models.vcenter import VCenterSource

                vc = db.get(VCenterSource, mapping_for_wave.vcenter_source_id)
                tgt = db.get(OCPTarget, mapping_for_wave.ocp_target_id)
                source_name = vc.name if vc is not None else None
                destination_name = tgt.name if tgt is not None else None
            ctx = WaveContext.from_settings(
                plan_id=plan_id,
                wave_number=wave_number,
                rationale=wave.get("rationale", ""),
                source_provider=source_name,
                destination_provider=destination_name,
            )
            try:
                yaml_text = generate_wave_yaml(ctx, vm_payloads, resolver=resolver)
            except MTVGenerationError as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Wave {wave_number}: {e}",
                ) from e
            zf.writestr(f"plan-{plan_id}-wave-{wave_number}.yaml", yaml_text)

    body = buf.getvalue()
    filename = f"plan-{plan_id}-mtv-bundle.zip"
    return Response(
        content=body,
        media_type="application/zip",
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
