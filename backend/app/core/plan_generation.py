"""BackgroundTask body for POST /api/plans.

Walks the new pipeline (Stages 0-7, see ``app.core.plan_pipeline``)
writing status transitions to the Plan row so the frontend's poll
endpoint reflects progress as it advances. Every failure path
releases the plan's VMs back to ``available`` via the shared
lifecycle helper.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.core import db as _db_module
from app.core.audit import record_audit
from app.core.vm_lifecycle import transition_to_available_from_failed_plan
from app.models.plan import MigrationPlan
from app.models.target import ResourceMapping
from app.models.vm import VM

logger = logging.getLogger(__name__)


_PIPELINE_STAGE_TO_STATUS = {
    "validating": ("validating", 5),
    "partitioning": ("partitioning", 15),
    "splitting": ("splitting", 30),
    "packing": ("packing", 45),
    "analyzing_concurrency": ("analyzing_concurrency", 55),
    "annotating": ("annotating", 70),
    "emitting_yaml": ("emitting_yaml", 90),
}


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

    Single helper so every failure path takes the same recovery shape:
    the failure surface in ``error_message`` matches what's in
    ``plan.error_message`` AND the VMs go back to ``available`` so
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


def run_simple_plan_generation(
    plan_id: int,
    *,
    vm_ids: list[int],
    ha_strategy: str = "spread",
    preclassification_enabled: bool = True,
    actor: str = "user",
) -> None:
    """Body of the BackgroundTask the POST /api/plans endpoint spawns.

    Walks Stages 0-7 of the new pipeline writing status transitions
    to the Plan row at each stage. On failure the typed-exception
    message is preserved verbatim in ``Plan.error_message`` so the
    operator sees the same string the lifespan startup health-check
    logs would surface.

    ``ha_strategy`` / ``preclassification_enabled`` are retained for
    back-compat with the PlanCreate schema but are no longer
    consulted — the new pipeline runs the deterministic stages
    unconditionally and the LLM never decides wave structure.
    """
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
