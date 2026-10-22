import logging
from datetime import datetime, timezone
from uuid import uuid4

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

from app.core import limits
from app.core.assessment import blocks_migration_type
from app.core.audit import record_audit
from app.core.config import settings as _app_settings
from app.core.db import get_db
from app.core.mapping_catalog import load_catalog
from app.core.mapping_validation import validate_plan_inputs
from app.core.mtv import (
    MappingResolver,
    MTVGenerationError,
    WaveContext,
    generate_wave_yaml,
)
from app.core.plan_chunking import chunk_partition, compose_series_name
from app.core.plan_generation import run_simple_plan_generation
from app.core.plan_membership import plan_partitions
from app.core.plan_pipeline import _vm_payload
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


def _excluded_entry(vm: VM, reason: str) -> dict:
    return {"vm_id": vm.id, "vm_name": vm.name, "reason": reason}


def _blocked_for_type(vms: list[VM], migration_type: str) -> list[tuple[VM, list[str]]]:
    """VMs whose migratability assessment blocks ``migration_type``,
    paired with the blocking finding ids."""
    out = []
    for vm in vms:
        ids = blocks_migration_type(vm.assessment, migration_type)
        if ids:
            out.append((vm, ids))
    return out


def _plan_batches(
    partitions: list[tuple[tuple, list[VM]]],
    max_per_plan: int,
) -> list[tuple[tuple, list[VM]]]:
    """Flatten ``partitions`` into the ordered list of plan rows a POST
    creates: each partition cut into batches of ≤ ``max_per_plan`` VMs
    (``app.core.plan_chunking.chunk_partition``), series order =
    partition order then batch order."""
    batches: list[tuple[tuple, list[VM]]] = []
    for key, p_vms in partitions:
        for chunk in chunk_partition(p_vms, max_per_plan):
            batches.append((key, chunk))
    return batches


def _mapping_rows_for_partitions(
    db: Session,
    partitions: list[tuple[tuple, list[VM]]],
    explicit_mapping_ids: list[int] | None,
) -> dict[tuple, list[ResourceMapping]]:
    """The ResourceMapping rows Stage 0 validates each partition
    against: every mapping for the partition's ``(vcenter, cluster)``
    pair, narrowed to ``explicit_mapping_ids`` when the caller sent a
    list (``[]`` means the operator opted out — no rows)."""
    out: dict[tuple, list[ResourceMapping]] = {}
    for key, _p_vms in partitions:
        vc_id, cluster_id, _namespace = key
        stmt = select(ResourceMapping).where(
            ResourceMapping.vcenter_source_id == vc_id,
            ResourceMapping.ocp_target_id == cluster_id,
        )
        if explicit_mapping_ids is not None:
            stmt = stmt.where(ResourceMapping.id.in_(explicit_mapping_ids))
        out[key] = list(db.scalars(stmt).all())
    return out


def _exclude_uncovered(
    partitions: list[tuple[tuple, list[VM]]],
    mapping_rows_by_key: dict[tuple, list[ResourceMapping]],
    excluded: list[dict],
) -> list[tuple[tuple, list[VM]]]:
    """``skip_ineligible`` for Stage 0: drop every VM with a mapping
    coverage gap into ``excluded`` (one entry per VM naming the
    unmapped network / datastore / namespace) and return the
    partitions with those VMs removed. Empty partitions disappear."""
    kept: list[tuple[tuple, list[VM]]] = []
    for key, p_vms in partitions:
        coverage = validate_plan_inputs(p_vms, mapping_rows_by_key.get(key, []))
        if coverage.ok:
            kept.append((key, p_vms))
            continue
        gaps_by_vm: dict[int, list[str]] = {}
        inventory_by_vm: dict[int, list[str]] = {}
        for g in coverage.gaps:
            if g.kind == "no_datastore":
                inventory_by_vm.setdefault(g.vm_id, []).append(g.source_value)
            else:
                gaps_by_vm.setdefault(g.vm_id, []).append(f"{g.kind} '{g.source_value}'")
        for vm in p_vms:
            parts = list(inventory_by_vm.get(vm.id, []))
            if vm.id in gaps_by_vm:
                parts.append("no mapping for " + ", ".join(gaps_by_vm[vm.id]))
            if parts:
                excluded.append(_excluded_entry(vm, "; ".join(parts)))
        remaining = [vm for vm in p_vms if vm.id not in gaps_by_vm and vm.id not in inventory_by_vm]
        if remaining:
            kept.append((key, remaining))
    return kept


def _raise_if_nothing_left(excluded: list[dict], selected_count: int) -> None:
    if excluded:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Every selected VM was excluded ({len(excluded)} of {selected_count}): "
                + "; ".join(f"{e['vm_name']}: {e['reason']}" for e in excluded[:8])
                + (f" (+{len(excluded) - 8} more)" if len(excluded) > 8 else "")
            ),
        )
    raise HTTPException(status_code=422, detail="No partitions resolved from selection.")


@router.post(
    "",
    response_model=PlanRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_plan(
    request: Request,
    payload: PlanCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Kick off async migration plan generation.

    There is no selection cap. The selection is fanned out by
    ``(source_vcenter_id, target_cluster_id, target_namespace)``
    partition, and each partition is cut into batches of at most
    ``settings.max_vms_per_plan`` VMs (``app.core.plan_chunking``).
    Every resulting MigrationPlan row gets its own background
    generation task (run sequentially, in series order) and its own
    MTV CR set at YAML-export time. Rows from one POST share
    ``series_id``; names are ``base-1``, ``base-2`` … series-wide, with
    the partition slug kept in the middle when the selection spans
    several partitions. A selection that yields exactly one plan keeps
    the user's name verbatim.

    The only ceiling is ``limits.MAX_VMS_PER_PLAN_SCOPE`` on the
    request body — a DoS guard, not a planning limit.

    With ``skip_ineligible`` the VMs that cannot be planned as selected
    (assessment-blocked for ``migration_type``, or unresolvable target)
    are left out and reported in ``excluded`` / persisted as
    ``excluded_vms`` on every row instead of failing the whole call.

    The response surfaces the first plan's PlanRead with a ``plans``
    list of every row created so callers see the full set. On LLM
    failure the verbatim typed-exception message lands in each plan's
    ``error_message``.
    """
    unique_ids = list(dict.fromkeys(payload.vm_ids))
    if not unique_ids:
        raise HTTPException(status_code=422, detail="vm_ids must contain at least one VM")
    ceiling = limits.MAX_VMS_PER_PLAN_SCOPE
    if len(unique_ids) > ceiling:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Selection exceeds the per-request ceiling: {len(unique_ids)} VMs requested, "
                f"max {ceiling} per request (MAX_VMS_PER_PLAN_SCOPE). Filter by vCenter or "
                "environment and generate in separate selections."
            ),
        )
    vms = list(db.scalars(select(VM).where(VM.id.in_(unique_ids))).all())
    known_ids = {v.id for v in vms}
    missing = [vid for vid in unique_ids if vid not in known_ids]
    if missing:
        raise HTTPException(status_code=404, detail=f"Unknown vm_ids: {missing}")

    excluded: list[dict] = []

    # Migratability gate. Finding a blocker here costs a re-selection;
    # finding it in the MTV console on cutover night costs the window.
    unfit = _blocked_for_type(vms, payload.migration_type)
    if unfit and not payload.override_assessment:
        if payload.skip_ineligible:
            for vm, ids in unfit:
                excluded.append(
                    _excluded_entry(vm, f"blocked for {payload.migration_type}: {', '.join(ids)}")
                )
            unfit_ids = {vm.id for vm, _ in unfit}
            vms = [vm for vm in vms if vm.id not in unfit_ids]
            unfit = []
        else:
            shown = "; ".join(f"{vm.name} ({', '.join(ids)})" for vm, ids in unfit[:8])
            more = f" (+{len(unfit) - 8} more)" if len(unfit) > 8 else ""
            warm_hint = (
                " For the warm-only findings, generate a cold plan for these VMs or enable "
                "Changed Block Tracking and re-import."
                if payload.migration_type == "warm"
                else ""
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{len(unfit)} selected VM(s) cannot migrate as a {payload.migration_type} plan: "
                    f"{shown}{more}. Deselect them (inventory filter: assessment_status=blocked), "
                    f"fix them in vSphere and re-import, or resend with override_assessment=true."
                    f"{warm_hint}"
                ),
            )
    if unfit:
        record_audit(
            db,
            action="plan.assessment_overridden",
            actor=request.headers.get("x-actor", "user"),
            resource_type="plan",
            resource_id=None,
            details={
                "migration_type": payload.migration_type,
                "vms": {vm.name: ids for vm, ids in unfit[:200]},
                "count": len(unfit),
            },
        )

    # Partition by (vcenter, cluster, namespace). Unresolvable VMs
    # surface immediately as a 422 (or are excluded) so the operator
    # fixes the mapping / override before the background tasks fire.
    partitions, unresolvable = _partition_vms_for_plans(vms, db)
    if unresolvable:
        if payload.skip_ineligible:
            for vm in unresolvable:
                excluded.append(_excluded_entry(vm, "no target cluster/namespace resolved"))
        else:
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
        _raise_if_nothing_left(excluded, len(unique_ids))

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

    # Per-partition mapping rows, resolved once and reused by every
    # batch of that partition. With ``skip_ineligible`` a VM the
    # mapping doesn't cover (Stage 0 gap) is excluded here rather
    # than failing the whole series at the per-batch check below.
    mapping_rows_by_key = _mapping_rows_for_partitions(db, partitions, explicit_mapping_ids)
    if payload.skip_ineligible:
        partitions = _exclude_uncovered(partitions, mapping_rows_by_key, excluded)
        if not partitions:
            _raise_if_nothing_left(excluded, len(unique_ids))

    # Cut every partition into plan-sized batches. Series numbering is
    # 1-based across ALL batches of ALL partitions.
    batches = _plan_batches(partitions, _app_settings.max_vms_per_plan)
    series_total = len(batches)
    series_id = uuid4().hex[:12]
    multi_partition = len(partitions) > 1

    # Build the plan rows in one pass, transitioning each batch's VMs to
    # ``planned`` against its own plan id. Everything is one transaction:
    # if any VM is already in another plan the whole call fails and no
    # background tasks fire.
    created_plans: list[MigrationPlan] = []
    bg_specs: list[tuple] = []
    try:
        for index, (key, b_vms) in enumerate(batches, start=1):
            vc_id, cluster_id, namespace = key
            b_vm_ids = [vm.id for vm in b_vms]
            mapping_rows = mapping_rows_by_key[key]

            # Stage 0 — pre-flight against this batch's mapping(s).
            coverage = validate_plan_inputs(b_vms, mapping_rows)
            if not coverage.ok:
                raise HTTPException(status_code=422, detail=coverage.render())

            vc_name = vcenters[vc_id].name if vc_id in vcenters else None
            cluster_name = targets[cluster_id].name if cluster_id in targets else None
            plan_name = compose_series_name(
                payload.name,
                vcenter_name=vc_name,
                cluster_name=cluster_name,
                namespace=namespace,
                index=index,
                total=series_total,
                multi_partition=multi_partition,
            )

            plan = MigrationPlan(
                name=plan_name,
                vm_ids=b_vm_ids,
                waves=[],
                model="",
                mapping_ids=[m.id for m in mapping_rows],
                migration_type=payload.migration_type,
                status="pending",
                progress_message="Queued",
                progress_percent=0,
                started_at=datetime.now(timezone.utc),
                series_id=series_id,
                series_index=index,
                series_total=series_total,
                excluded_vms=list(excluded),
            )
            db.add(plan)
            db.flush()  # need plan.id before the lifecycle transition + bg task
            transition_to_planned(b_vm_ids, plan.id, db)
            created_plans.append(plan)
            bg_specs.append((plan.id, b_vm_ids))
    except LifecycleTransitionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except HTTPException:
        db.rollback()
        raise

    if excluded:
        record_audit(
            db,
            action="plan.vms_excluded",
            actor=request.headers.get("x-actor", "user"),
            resource_type="plan",
            resource_id=created_plans[0].id,
            details={
                "series_id": series_id,
                "migration_type": payload.migration_type,
                "count": len(excluded),
                "vms": excluded[:200],
            },
        )

    db.commit()
    for plan in created_plans:
        db.refresh(plan)

    for plan_id, b_vm_ids in bg_specs:
        background_tasks.add_task(
            run_simple_plan_generation,
            plan_id,
            vm_ids=b_vm_ids,
            ha_strategy=payload.ha_strategy,
            preclassification_enabled=payload.preclassification_enabled,
        )

    # Response: the first plan's PlanRead at the top level (legacy
    # callers read id/status/etc directly) PLUS a ``plans`` list with
    # every row created so the wizard can follow all of them.
    first = created_plans[0]
    body = PlanRead.model_validate(first).model_dump(mode="json")
    body["groups"] = []
    body["groups_formed"] = 0
    body["method"] = ""
    body["attempts"] = 0
    body["plans"] = [PlanRead.model_validate(p).model_dump(mode="json") for p in created_plans]
    body["plan_count"] = len(created_plans)
    body["series_id"] = series_id
    body["excluded"] = excluded
    return body


@router.post("/preview", response_model=PlanPreviewResponse)
def preview_plan_partitions(payload: PlanCreate, db: Session = Depends(get_db)) -> dict:
    """Show how the selection will fan out into plan rows without
    creating any. ``groups`` is the per-``(source_vcenter,
    target_cluster, target_namespace)`` partition view; ``plans`` is
    the row-level view after batching — the exact names and VM counts
    POST /api/plans would create — and ``excluded`` is every VM that
    ``skip_ineligible`` would leave out (assessment-blocked for
    ``migration_type`` ∪ ``unresolved`` ∪ Stage 0 mapping gaps against
    the same mapping rows POST would use — ``mapping_ids`` semantics
    match the create call).

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

    excluded: list[dict] = []
    blocked_ids: set[int] = set()
    if not payload.override_assessment:
        for vm, ids in _blocked_for_type(vms, payload.migration_type):
            blocked_ids.add(vm.id)
            excluded.append(
                _excluded_entry(vm, f"blocked for {payload.migration_type}: {', '.join(ids)}")
            )

    by_key: dict[tuple, dict] = {}
    vms_by_key: dict[tuple, list[VM]] = {}
    unresolved: list[dict] = []
    for vm in vms:
        r = resolutions.get(vm.id)
        if r is None or r.cluster_id is None or not r.namespace:
            reasons = list(r.reasons) if r is not None else ["resolution unavailable"]
            unresolved.append({"vm_id": vm.id, "vm_name": vm.name, "reasons": reasons})
            if vm.id not in blocked_ids:
                excluded.append(_excluded_entry(vm, "no target cluster/namespace resolved"))
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
        if vm.id not in blocked_ids:
            vms_by_key.setdefault(key, []).append(vm)
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

    # Row-level view: the same batching + naming POST /api/plans applies,
    # after the same Stage 0 coverage screen ``skip_ineligible`` performs.
    partitions = [(key, vms_by_key[key]) for key in by_key if key in vms_by_key]
    explicit_ids = list(payload.mapping_ids) if payload.mapping_ids is not None else None
    partitions = _exclude_uncovered(
        partitions, _mapping_rows_for_partitions(db, partitions, explicit_ids), excluded
    )
    batch_size = _app_settings.max_vms_per_plan
    batches = _plan_batches(partitions, batch_size)
    multi_partition = len(partitions) > 1
    plans: list[dict] = []
    for index, ((vc_id, cluster_id, namespace), b_vms) in enumerate(batches, start=1):
        vc_name = vcenters[vc_id].name if vc_id in vcenters else None
        cluster_name = targets[cluster_id].name if cluster_id in targets else None
        plans.append(
            {
                "name": compose_series_name(
                    payload.name,
                    vcenter_name=vc_name,
                    cluster_name=cluster_name,
                    namespace=namespace,
                    index=index,
                    total=len(batches),
                    multi_partition=multi_partition,
                ),
                "vm_count": len(b_vms),
                "series_index": index,
                "source_vcenter_id": vc_id,
                "source_vcenter_name": vc_name,
                "target_cluster_id": cluster_id,
                "target_cluster_name": cluster_name,
                "target_namespace": namespace,
            }
        )

    return {
        "total_vms": len(vms),
        "resolvable": sum(g["vm_count"] for g in by_key.values()),
        "unresolvable": len(unresolved),
        "groups": list(by_key.values()),
        "unresolved": unresolved,
        "plans": plans,
        "plan_count": len(plans),
        "batch_size": batch_size,
        "excluded": excluded,
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


def _plans_with_partition(db: Session, plans: list[MigrationPlan]) -> list[dict]:
    """PlanRead bodies with the derived ``partition`` filled in."""
    partitions = plan_partitions(db, plans)
    out: list[dict] = []
    for plan in plans:
        body = PlanRead.model_validate(plan).model_dump(mode="json")
        partition = partitions.get(plan.id)
        body["partition"] = partition.to_dict() if partition else None
        out.append(body)
    return out


@router.get("", response_model=list[PlanRead])
def list_plans(
    db: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=500),
    series_id: str | None = Query(default=None, max_length=32),
) -> list[dict]:
    # Newest first; within a batched series (same created_at to the
    # millisecond) keep the rows in series order.
    stmt = select(MigrationPlan)
    if series_id:
        stmt = stmt.where(MigrationPlan.series_id == series_id)
    stmt = stmt.order_by(MigrationPlan.created_at.desc(), MigrationPlan.series_index.asc()).limit(
        limit
    )
    return _plans_with_partition(db, list(db.scalars(stmt).all()))


@router.get("/{plan_id}", response_model=PlanRead)
def get_plan(plan_id: int, db: Session = Depends(get_db)) -> dict:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return _plans_with_partition(db, [plan])[0]


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
    # Row lock: serializes against a concurrent manual edit of this plan.
    plan = db.get(MigrationPlan, plan_id, with_for_update=True)
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
    # Row lock: ``plan.vm_ids`` must not change under a concurrent edit.
    plan = db.get(MigrationPlan, plan_id, with_for_update=True)
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

    vm_payloads = [_vm_payload(vms_by_id[vid]) for vid in vm_ids]

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
            catalog=load_catalog(db, mapping_for_wave.ocp_target_id),
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
        migration_type=plan.migration_type or "cold",
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

    # Same name the bundle uses, so the bundle README's `plan-N-wave-*.yaml` loop
    # also matches a wave downloaded on its own.
    filename = f"plan-{plan_id}-wave-{wave_number}.yaml"
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
    bundled: list[tuple[int, int, str | None, str | None]] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for wave in sorted(plan.waves, key=lambda w: w.get("wave_number", 0)):
            wave_number = wave.get("wave_number")
            if wave_number is None:
                continue
            vm_ids: list[int] = list(wave.get("vm_ids") or [])
            if not vm_ids:
                continue
            vms_by_id = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(vm_ids))).all()}
            vm_payloads = [_vm_payload(vms_by_id[vid]) for vid in vm_ids if vid in vms_by_id]
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
                    catalog=load_catalog(db, mapping_for_wave.ocp_target_id),
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
                migration_type=plan.migration_type or "cold",
            )
            try:
                yaml_text = generate_wave_yaml(ctx, vm_payloads, resolver=resolver)
            except MTVGenerationError as e:
                raise HTTPException(
                    status_code=422,
                    detail=f"Wave {wave_number}: {e}",
                ) from e
            zf.writestr(f"plan-{plan_id}-wave-{wave_number}.yaml", yaml_text)
            bundled.append((wave_number, len(vm_payloads), source_name, destination_name))
        zf.writestr("README.md", _bundle_readme(plan, bundled))

    body = buf.getvalue()
    filename = f"plan-{plan_id}-mtv-bundle.zip"
    return Response(
        content=body,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _bundle_readme(plan: MigrationPlan, bundled: list[tuple]) -> str:
    """Apply instructions shipped inside the YAML bundle.

    VirtValidate never touches the cluster: the operator applies these with
    their own ``oc`` and their own credentials, so the bundle has to stand on
    its own — prerequisites, order, and how to check it before it counts.
    """
    ns = _app_settings.mtv_namespace
    sources = sorted({b[2] for b in bundled if b[2]})
    destinations = sorted({b[3] for b in bundled if b[3]})
    lines = [
        f"# MTV bundle — plan {plan.id}: {plan.name}",
        "",
        f"Generated by VirtValidate. {len(bundled)} wave(s), "
        f"{sum(b[1] for b in bundled)} VM(s), migration type **{plan.migration_type}**.",
        "Every document here passed offline validation against the forklift "
        "v2.12.1 CRD schemas (MTV 2.12). That proves shape, not that the things "
        "it references exist on your cluster — step 2 does that.",
        "",
        "## 1. Prerequisites on the target cluster",
        "",
        f"- MTV operator installed; these resources go in namespace `{ns}`.",
        "- Provider CRs **already created by you** (they hold vCenter credentials, "
        "which VirtValidate never sees):",
        *[f"  - source provider `{name}` in `{ns}`" for name in sources],
        *[f"  - destination provider `{name}` in `{ns}`" for name in destinations],
        "- Every target namespace, NetworkAttachmentDefinition and StorageClass "
        "named in the maps exists. A NAD must be in the VM's target namespace "
        "(or `default`).",
    ]
    if plan.migration_type == "warm":
        lines += [
            "- **Warm migration:** Changed Block Tracking enabled on every VM *and* "
            "every disk, VMware Tools running, and on Windows the VSS and VMware "
            "Snapshot Provider services set to Manual or Automatic. Do not take "
            "snapshots once a migration has started.",
        ]
    lines += [
        "- More than 10 VMs from one ESXi host in a plan requires raising that "
        "host's NFC service memory.",
        "",
        "## 2. Validate against your cluster (changes nothing)",
        "",
        "```bash",
        f'for f in plan-{plan.id}-wave-*.yaml; do oc apply --dry-run=server -f "$f" || break; done',
        "```",
        "",
        "## 3. Apply one wave at a time, in order",
        "",
        "Each file holds a NetworkMap, a StorageMap and a Plan. Applying it "
        "creates the plan; it does **not** start a migration.",
        "",
        "```bash",
        f"oc apply -f plan-{plan.id}-wave-1.yaml",
        f"oc get plan -n {ns} vv-plan-{plan.id}-wave-1 -o jsonpath='{{.status.conditions}}'",
        "```",
        "",
        "Wait for the plan's `Ready` condition, review any `Warning`/`Critical` "
        "concerns MTV reports on its VMs, then start it from the MTV console, or:",
        "",
        "```bash",
        "cat <<EOF | oc apply -f -",
        "apiVersion: forklift.konveyor.io/v1beta1",
        "kind: Migration",
        "metadata:",
        f"  name: vv-plan-{plan.id}-wave-1-run",
        f"  namespace: {ns}",
        "spec:",
        "  plan:",
        f"    name: vv-plan-{plan.id}-wave-1",
        f"    namespace: {ns}",
        "EOF",
        "```",
        "",
        "Validate the wave in VirtValidate before moving to the next one.",
        "",
        "## Waves",
        "",
        "| Wave | VMs | File |",
        "|---:|---:|---|",
        *[f"| {b[0]} | {b[1]} | `plan-{plan.id}-wave-{b[0]}.yaml` |" for b in bundled],
        "",
    ]
    return "\n".join(lines)


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
