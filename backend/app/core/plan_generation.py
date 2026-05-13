"""Async migration plan generation + revision tracking.

Mirrors the capture / validation / categorization patterns: a thread-
safe in-memory task store + a BackgroundTask entry point that walks
the pipeline (aggregate → LLM → parse → persist) updating progress
along the way.

Per-wave VM-move lives here too because the revision-creation logic
(``apply_move_vm``) is data-shaped, not HTTP-shaped — the API layer
is just a thin wrapper.
"""

from __future__ import annotations

import logging
import threading
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.audit import record_audit
from app.core.baseline import synthesize_profile
from app.core.chunked_planner import (
    HierarchicalPlanResult,
)
from app.core.chunked_planner import (
    generate_plan as run_hierarchical_plan,
)
from app.core.strategy_planner import StrategyPlannerError
from app.core.vm_lifecycle import transition_to_available_from_failed_plan
from app.models.chunk import PlanChunk
from app.models.plan import MigrationPlan, PlanningStrategy
from app.models.target import ResourceMapping
from app.models.vcenter import VCenterSource
from app.models.vm import VM, BaselineSnapshot

logger = logging.getLogger(__name__)


PlanGenerationStatus = Literal["running", "completed", "failed"]
PlanGenerationStep = Literal[
    "queued",
    "aggregating_data",
    "chunking",
    "planning_chunks",
    "planning_single_shot",
    "assembling",
    "reviewing",
    "llm_reasoning",  # legacy alias kept for back-compat with old UI
    "parsing_response",
    "validating",
    "persisting",
    "completed",
    "failed",
]


# ---------------------------------------------------------------------------
# Task store
# ---------------------------------------------------------------------------
@dataclass
class PlanGenerationTask:
    task_id: str
    status: PlanGenerationStatus = "running"
    current_step: PlanGenerationStep = "queued"
    progress_percent: int = 0
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    plan_id: Optional[int] = None
    error: Optional[str] = None
    # Hierarchical-planner progress fields. Optional so the legacy
    # single-shot path leaves them None.
    chunks_total: Optional[int] = None
    chunks_complete: Optional[int] = None
    current_chunk: Optional[str] = None
    elapsed_seconds: Optional[int] = None
    estimated_remaining_seconds: Optional[int] = None
    path_taken: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("started_at", "completed_at"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        return d


class PlanGenerationTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, PlanGenerationTask] = {}
        self._lock = threading.RLock()

    def create(self) -> PlanGenerationTask:
        task = PlanGenerationTask(task_id=str(uuid.uuid4()))
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[PlanGenerationTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def update(
        self,
        task_id: str,
        *,
        current_step: PlanGenerationStep | None = None,
        progress_percent: int | None = None,
        **fields: Any,
    ) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            if current_step is not None:
                t.current_step = current_step
            if progress_percent is not None:
                t.progress_percent = progress_percent
            for k, v in fields.items():
                if hasattr(t, k):
                    setattr(t, k, v)

    def mark_completed(self, task_id: str, *, plan_id: int) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "completed"
            t.current_step = "completed"
            t.progress_percent = 100
            t.plan_id = plan_id
            t.completed_at = datetime.now(timezone.utc)

    def mark_failed(self, task_id: str, *, error: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = "failed"
            t.current_step = "failed"
            t.error = error
            t.completed_at = datetime.now(timezone.utc)


task_store = PlanGenerationTaskStore()


def _unmapped_source_resources(
    mapping: ResourceMapping, vms: list[VM]
) -> tuple[list[str], list[str]]:
    """Return (unmapped_networks, unmapped_datastores) — source vSphere
    resources referenced by an in-scope VM that the mapping doesn't
    resolve to a target. Same pre-check the MTV YAML emitter runs at
    download time; lifting it earlier means plan generation fails
    fast with a clear error instead of persisting a plan that can't
    produce valid YAML."""
    mapped_nets: set[str] = set()
    for r in mapping.network_mappings or []:
        src = (r or {}).get("source_network")
        tgt = ((r or {}).get("target_network_name") or "").strip()
        if src and tgt:
            mapped_nets.add(src)
    mapped_ds: set[str] = set()
    for r in mapping.storage_mappings or []:
        src = (r or {}).get("source_datastore")
        tgt = ((r or {}).get("target_storage_class") or "").strip()
        if src and tgt:
            mapped_ds.add(src)
    referenced_nets: set[str] = set()
    referenced_ds: set[str] = set()
    for vm in vms:
        for n in vm.vsphere_networks or []:
            if n:
                referenced_nets.add(n)
        for d in vm.vsphere_datastores or []:
            if d:
                referenced_ds.add(d)
    return (
        sorted(referenced_nets - mapped_nets),
        sorted(referenced_ds - mapped_ds),
    )


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------
def resolve_scope(db: Session, scope: dict) -> list[VM]:
    """Apply the wizard's scope filter and return the matching VMs.

    The scope dict mirrors :class:`PlanScopeFilter`. Most-specific axis
    wins — explicit vm_ids > vCenter > environment / app_hint > all.
    """
    vm_ids = scope.get("vm_ids") or []
    if vm_ids:
        return list(db.scalars(select(VM).where(VM.id.in_(vm_ids)).order_by(VM.id)).all())
    stmt = select(VM).order_by(VM.id)
    if scope.get("source_vcenter_id") is not None:
        stmt = stmt.where(VM.source_vcenter_id == scope["source_vcenter_id"])
    if scope.get("environment"):
        stmt = stmt.where(VM.environment == scope["environment"])
    if scope.get("application_hint"):
        stmt = stmt.where(VM.application_hint == scope["application_hint"])
    return list(db.scalars(stmt).all())


def assemble_vm_profiles(db: Session, vms: list[VM]) -> list[dict]:
    """Build the lightweight VM payload the strategy planner sends to
    the LLM. Same shape as the legacy planner so existing tests keep
    working."""
    profiles: list[dict] = []
    for vm in vms:
        snapshots = list(
            db.scalars(
                select(BaselineSnapshot)
                .where(BaselineSnapshot.vm_id == vm.id)
                .order_by(BaselineSnapshot.collected_at.asc())
            ).all()
        )
        profile = synthesize_profile(vm.id, snapshots)
        profiles.append(
            {
                "vm_id": vm.id,
                "name": vm.name,
                "role": vm.role or "",
                "os_family": vm.os_family or "",
                "environment": vm.environment or "",
                "owner": vm.owner or "",
                "application_hint": vm.application_hint or "",
                "source_vcenter_id": vm.source_vcenter_id,
                "vsphere_networks": list(vm.vsphere_networks or []),
                "vsphere_datastores": list(vm.vsphere_datastores or []),
                "target_namespace": vm.target_namespace or "",
                "target_storage_class": vm.target_storage_class or "",
                "target_network_attachment": vm.target_network_attachment or "",
                "baseline": profile.model_dump(mode="json"),
            }
        )
    return profiles


# ---------------------------------------------------------------------------
# BackgroundTask entry point
# ---------------------------------------------------------------------------
def run_plan_generation(
    task_id: str,
    *,
    plan_name: str,
    strategy_id: int,
    scope: dict,
    actor: str = "user",
    mapping_id: int | None = None,
) -> None:
    """Body of the FastAPI BackgroundTask the generate endpoint spawns."""
    db = _db_module.SessionLocal()
    try:
        task_store.update(task_id, current_step="aggregating_data", progress_percent=10)

        strategy = db.get(PlanningStrategy, strategy_id)
        if strategy is None:
            task_store.mark_failed(task_id, error=f"Strategy {strategy_id} not found")
            return
        mapping_warnings: list[str] = []
        mapping: ResourceMapping | None = None
        if mapping_id is not None:
            mapping = db.get(ResourceMapping, mapping_id)
            if mapping is None:
                task_store.mark_failed(task_id, error=f"Resource mapping {mapping_id} not found")
                return
            if mapping.status.value == "needs_review":
                mapping_warnings.append(
                    f"Mapping {mapping.id} ({mapping.name!r}) needs review — "
                    "drift detected against the target cluster's discovered "
                    "resources. Re-run discovery and review before applying."
                )
        else:
            mapping_warnings.append(
                "Plan generated without a resource_mapping — MTV YAML export "
                "will fall back to per-VM target_* fields and may reference "
                "placeholder resource names. Attach a mapping for production use."
            )
        vms = resolve_scope(db, scope)
        if not vms:
            task_store.mark_failed(task_id, error="Scope filter matched zero VMs")
            return

        # Hard-fail when an attached mapping leaves source resources
        # unmapped. The prior behavior emitted a warning and produced
        # placeholder YAML; that surfaced as a confusing ``oc apply``
        # failure later. Fail-fast at generation time so the operator
        # can fix the mapping before any plan persists.
        if mapping is not None:
            unmapped_nets, unmapped_ds = _unmapped_source_resources(mapping, vms)
            if unmapped_nets or unmapped_ds:
                parts: list[str] = []
                if unmapped_nets:
                    sample = ", ".join(sorted(unmapped_nets)[:5])
                    extra = f" (+{len(unmapped_nets) - 5} more)" if len(unmapped_nets) > 5 else ""
                    parts.append(
                        f"{len(unmapped_nets)} source networks unmapped " f"({sample}{extra})"
                    )
                if unmapped_ds:
                    sample = ", ".join(sorted(unmapped_ds)[:5])
                    extra = f" (+{len(unmapped_ds) - 5} more)" if len(unmapped_ds) > 5 else ""
                    parts.append(
                        f"{len(unmapped_ds)} source datastores unmapped " f"({sample}{extra})"
                    )
                msg = (
                    "Cannot generate plan: "
                    + "; ".join(parts)
                    + ". Open the mapping editor and assign target "
                    + (
                        "networks."
                        if unmapped_nets and not unmapped_ds
                        else "datastores."
                        if unmapped_ds and not unmapped_nets
                        else "resources."
                    )
                )
                task_store.mark_failed(task_id, error=msg)
                return
        profiles = assemble_vm_profiles(db, vms)

        # Build a vcenter-id → classification lookup so the chunker
        # can hard-partition by classification without doing a second
        # query per VM.
        vc_rows = list(db.scalars(select(VCenterSource)).all())
        classification_by_vc = {row.id: row.classification_level.value for row in vc_rows}

        # Hand off to the hierarchical planner. Progress callbacks
        # update the task store so the UI's poll endpoint reflects
        # chunking → per-chunk → assembly → review stages.
        def _progress(**kw) -> None:
            stage = kw.get("stage")
            chunks_total = kw.get("chunks_total")
            chunks_complete = kw.get("chunks_complete")
            current_chunk = kw.get("current_chunk")
            elapsed = kw.get("elapsed_seconds")
            # Map stage → progress_percent so the legacy progress bar
            # still moves smoothly.
            pct = _stage_to_percent(stage, chunks_total, chunks_complete)
            update_kwargs: dict = {"current_step": stage}
            if pct is not None:
                update_kwargs["progress_percent"] = pct
            if chunks_total is not None:
                update_kwargs["chunks_total"] = chunks_total
            if chunks_complete is not None:
                update_kwargs["chunks_complete"] = chunks_complete
            if current_chunk is not None:
                update_kwargs["current_chunk"] = current_chunk
            if elapsed is not None:
                update_kwargs["elapsed_seconds"] = elapsed
            task_store.update(task_id, **update_kwargs)

        try:
            result: HierarchicalPlanResult = run_hierarchical_plan(
                vms=vms,
                vm_profiles=profiles,
                strategy=strategy,
                mappings=(db.get(ResourceMapping, mapping_id) if mapping_id else None),
                classification_by_vcenter=classification_by_vc,
                progress_cb=_progress,
            )
        except StrategyPlannerError as e:
            logger.warning("plan generation failed: %s", e)
            task_store.mark_failed(task_id, error=str(e))
            return

        task_store.update(task_id, current_step="persisting", progress_percent=95)
        merged_warnings = list(result.warnings) + mapping_warnings
        plan = MigrationPlan(
            name=plan_name,
            vm_ids=[p["vm_id"] for p in profiles],
            waves=result.waves,
            summary=result.plan_summary or None,
            model=result.model or "",
            mapping_id=mapping_id,
            generation_prompt=result.generation_prompt or None,
            generation_response=result.generation_response or None,
            plan_summary=result.plan_summary or None,
            rationale=result.rationale or None,
            warnings=merged_warnings,
            next_actions=result.next_actions,
            revision_number=1,
        )
        db.add(plan)
        db.flush()  # need plan.id before persisting chunks

        for idx, chunk_dict in enumerate(result.chunks):
            db.add(
                PlanChunk(
                    plan_id=plan.id,
                    chunk_id=chunk_dict["chunk_id"],
                    sequence_index=idx,
                    label=chunk_dict.get("label") or "",
                    reason_for_chunk=chunk_dict.get("reason_for_chunk") or "",
                    partition_key=chunk_dict.get("partition_key") or {},
                    sub_key=chunk_dict.get("sub_key") or {},
                    hints=chunk_dict.get("hints") or {},
                    vm_ids=chunk_dict.get("vm_ids") or [],
                    sequence_dependencies=(chunk_dict.get("sequence_dependencies") or []),
                    chunk_rationale=chunk_dict.get("chunk_rationale") or None,
                    chunk_risk_level=chunk_dict.get("chunk_risk_level") or None,
                    wave_numbers=chunk_dict.get("wave_numbers") or [],
                )
            )
        db.commit()
        db.refresh(plan)

        record_audit(
            db,
            action="plan.generated",
            actor=actor,
            resource_type="plan",
            resource_id=plan.id,
            details={
                "name": plan.name,
                "strategy_id": strategy.id,
                "vm_count": len(plan.vm_ids),
                "wave_count": len(plan.waves),
                "chunk_count": len(result.chunks),
                "warning_count": len(plan.warnings),
                "path_taken": result.path_taken,
            },
        )
        db.commit()

        task_store.update(task_id, path_taken=result.path_taken)
        task_store.mark_completed(task_id, plan_id=plan.id)
        logger.info(
            "plan %s generated via %s: %d waves across %d chunks for %d VMs",
            plan.id,
            result.path_taken,
            len(plan.waves),
            len(result.chunks),
            len(plan.vm_ids),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Simple (non-strategy) async runner used by POST /api/plans
# ---------------------------------------------------------------------------
def _update_plan_status(
    plan_id: int,
    *,
    status_value: str | None = None,
    progress_message: str | None = None,
    progress_percent: int | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    """Apply a single status transition to a Plan row.

    Each transition is its own short-lived session so the polling
    endpoint sees the update as soon as the planner advances —
    deferring all writes to the end of the BackgroundTask would
    leave the UI staring at progress_percent=0 for minutes.
    """
    db = _db_module.SessionLocal()
    try:
        plan = db.get(MigrationPlan, plan_id)
        if plan is None:
            return
        if status_value is not None:
            plan.status = status_value
        if progress_message is not None:
            plan.progress_message = progress_message
        if progress_percent is not None:
            plan.progress_percent = progress_percent
        if error_message is not None:
            plan.error_message = error_message
        if completed_at is not None:
            plan.completed_at = completed_at
        db.commit()
    finally:
        db.close()


def _fail_plan_with_lifecycle_release(plan_id: int, error: str) -> None:
    """Mark the plan ``failed`` and release its VMs from ``planned``.

    Single helper so both branches of run_simple_plan_generation (and
    the new pipeline once it lands) take the exact same recovery path:
    the failure surface in ``error_message`` matches what's in
    ``plan.error_message``, AND the VMs go back to ``available`` so
    operators can immediately retry.
    """
    db = _db_module.SessionLocal()
    try:
        plan = db.get(MigrationPlan, plan_id)
        if plan is None:
            return
        plan.status = "failed"
        plan.error_message = error
        plan.completed_at = datetime.now(timezone.utc)
        transition_to_available_from_failed_plan(
            plan.id, list(plan.vm_ids or []), db, actor="system"
        )
        db.commit()
    finally:
        db.close()


_PIPELINE_STAGE_TO_STATUS = {
    "validating": ("validating", 5),
    "partitioning": ("partitioning", 15),
    "splitting": ("splitting", 30),
    "packing": ("packing", 45),
    "analyzing_concurrency": ("analyzing_concurrency", 55),
    "annotating": ("annotating", 70),
    "emitting_yaml": ("emitting_yaml", 90),
}


def run_simple_plan_generation(
    plan_id: int,
    *,
    vm_ids: list[int],
    ha_strategy: str = "spread",
    preclassification_enabled: bool = True,
    actor: str = "user",
) -> None:
    """Body of the BackgroundTask the POST /api/plans endpoint spawns.

    Walks the new pipeline (Stages 0-7) writing status transitions to
    the Plan row at each stage. On failure the typed-exception message
    is preserved verbatim in ``Plan.error_message`` so the operator
    sees the same string the lifespan startup health-check logs would
    surface. VMs roll back to ``lifecycle_state=available`` on every
    failure path via the shared helper.

    ``ha_strategy`` / ``preclassification_enabled`` are retained for
    back-compat with the existing PlanCreate schema but are no longer
    consulted — the new pipeline runs the deterministic stages
    unconditionally and the LLM never decides wave structure.
    """
    import asyncio

    from app.core.llm.factory import get_llm_backend
    from app.core.mtv import MTVGenerationError
    from app.core.plan_pipeline import PlanValidationError, run_pipeline

    started = datetime.now(timezone.utc)
    _update_plan_status(
        plan_id,
        status_value="validating",
        progress_message="Loading VMs",
        progress_percent=5,
    )

    db = _db_module.SessionLocal()
    try:
        unique_ids = list(dict.fromkeys(vm_ids))
        vms = list(db.scalars(select(VM).where(VM.id.in_(unique_ids))).all())
        missing = [vid for vid in unique_ids if vid not in {v.id for v in vms}]
        if missing:
            _fail_plan_with_lifecycle_release(plan_id, f"Unknown vm_ids: {missing}")
            return

        plan_row = db.get(MigrationPlan, plan_id)
        if plan_row is None:
            return
        mapping = (
            db.get(ResourceMapping, plan_row.mapping_id)
            if plan_row.mapping_id is not None
            else None
        )

        def _progress_cb(stage: str) -> None:
            status_value, pct = _PIPELINE_STAGE_TO_STATUS.get(stage, (stage, None))
            _update_plan_status(
                plan_id,
                status_value=status_value,
                progress_message=f"Stage: {stage}",
                progress_percent=pct,
            )

        backend = get_llm_backend()
        try:
            pipeline_result = asyncio.run(
                run_pipeline(
                    vms,
                    mapping,
                    plan_id=plan_id,
                    backend=backend,
                    progress_cb=_progress_cb,
                )
            )
        except PlanValidationError as e:
            logger.warning("Plan %d validation failed: %s", plan_id, e)
            _fail_plan_with_lifecycle_release(plan_id, str(e))
            return
        except MTVGenerationError as e:  # pragma: no cover — Stage 0 should catch
            _fail_plan_with_lifecycle_release(plan_id, f"MTV YAML generation: {e}")
            return
        except Exception as e:  # noqa: BLE001 — surface verbatim to operator
            logger.exception("Plan %d pipeline error", plan_id)
            _fail_plan_with_lifecycle_release(plan_id, str(e))
            return

        plan = db.get(MigrationPlan, plan_id)
        if plan is None:
            return
        plan.vm_ids = unique_ids
        plan.waves = [aw.to_dict() for aw in pipeline_result.waves]
        plan.summary = None
        plan.model = getattr(backend, "default_model", "") or ""
        plan.status = "complete"
        plan.progress_message = "Done"
        plan.progress_percent = 100
        plan.completed_at = datetime.now(timezone.utc)
        db.commit()

        method_counts: dict[str, int] = {}
        for m in pipeline_result.method_per_wave.values():
            method_counts[m] = method_counts.get(m, 0) + 1
        record_audit(
            db,
            action="plan.generated",
            actor=actor,
            resource_type="plan",
            resource_id=plan.id,
            details={
                "vm_count": len(plan.vm_ids),
                "wave_count": len(plan.waves),
                "method_per_wave": pipeline_result.method_per_wave,
                "method_counts": method_counts,
                "elapsed_seconds": int((datetime.now(timezone.utc) - started).total_seconds()),
            },
        )
        db.commit()
        logger.info(
            "plan %d generated: %d waves for %d VMs (methods=%s)",
            plan.id,
            len(plan.waves),
            len(plan.vm_ids),
            method_counts,
        )
    finally:
        db.close()


def _stage_to_percent(
    stage: str | None,
    chunks_total: int | None,
    chunks_complete: int | None,
) -> int | None:
    """Map the hierarchical-planner stage to a flat percent so the
    existing UI progress bar keeps making sense across pipelines."""
    if stage == "chunking":
        return 5
    if stage == "planning_single_shot":
        return 50
    if stage == "planning_chunks":
        if chunks_total and chunks_total > 0 and chunks_complete is not None:
            # Chunk planning owns 10-85% of the bar.
            return 10 + int(75 * chunks_complete / chunks_total)
        return 10
    if stage == "assembling":
        return 87
    if stage == "reviewing":
        return 92
    if stage == "completed":
        return 100
    return None


# ---------------------------------------------------------------------------
# Per-wave VM move + revision creation
# ---------------------------------------------------------------------------
class PlanRevisionError(RuntimeError):
    """Raised when a per-wave action can't be applied — usually a
    target wave that doesn't exist or a vm_id that isn't in any wave."""


def apply_move_vm(
    db: Session,
    plan: MigrationPlan,
    *,
    vm_id: int,
    target_wave_number: int,
    actor: str,
    note: str | None = None,
) -> MigrationPlan:
    """Create a new plan revision with one VM moved between waves.

    The original plan is preserved; the new plan references it via
    ``supersedes_plan_id`` so the revision history walks cleanly.
    Empty waves are left in place (operators may want them as
    placeholders); cleanup is a separate operator decision.
    """
    if vm_id not in (plan.vm_ids or []):
        raise PlanRevisionError(f"vm_id {vm_id} is not in plan {plan.id}")
    waves = deepcopy(plan.waves or [])
    target_wave = next((w for w in waves if w.get("wave_number") == target_wave_number), None)
    if target_wave is None:
        raise PlanRevisionError(
            f"target_wave_number {target_wave_number} not found in plan {plan.id}"
        )
    source_wave = next(
        (w for w in waves if vm_id in (w.get("vm_ids") or [])),
        None,
    )
    if source_wave is None:
        raise PlanRevisionError(f"vm_id {vm_id} not assigned to any wave in plan {plan.id}")
    if source_wave.get("wave_number") == target_wave_number:
        raise PlanRevisionError(f"vm_id {vm_id} is already in wave {target_wave_number}")

    source_wave["vm_ids"] = [v for v in source_wave["vm_ids"] if v != vm_id]
    target_wave["vm_ids"] = [*target_wave.get("vm_ids", []), vm_id]

    revision = MigrationPlan(
        name=plan.name,
        vm_ids=list(plan.vm_ids or []),
        waves=waves,
        summary=plan.summary,
        model=plan.model,
        generation_prompt=plan.generation_prompt,
        generation_response=plan.generation_response,
        plan_summary=plan.plan_summary,
        rationale=plan.rationale,
        warnings=list(plan.warnings or []),
        next_actions=list(plan.next_actions or []),
        supersedes_plan_id=plan.id,
        revision_number=(plan.revision_number or 1) + 1,
    )
    db.add(revision)
    db.commit()
    db.refresh(revision)

    record_audit(
        db,
        action="plan.revision.move_vm",
        actor=actor,
        resource_type="plan",
        resource_id=revision.id,
        details={
            "vm_id": vm_id,
            "from_wave": source_wave.get("wave_number"),
            "to_wave": target_wave_number,
            "supersedes_plan_id": plan.id,
            "revision_number": revision.revision_number,
            "note": note,
        },
    )
    db.commit()
    return revision
