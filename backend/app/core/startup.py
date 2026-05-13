"""One-shot tasks the FastAPI lifespan runs once on each app boot.

Currently:

  - :func:`fail_orphan_plans` — find any plan stuck in an in-progress
    status from a prior process (the BackgroundTask that was running
    it died with the container) and mark it ``failed`` so the
    progress poll terminates and the VM lifecycle releases them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.vm_lifecycle import transition_to_available_from_failed_plan
from app.models.plan import MigrationPlan

logger = logging.getLogger(__name__)

# Statuses that mean "a background task was running and got killed".
# We list them explicitly rather than computing the complement of
# (complete, failed, migrated) so a future status name (e.g.
# ``cancelled``) doesn't get auto-marked as orphan.
_ORPHAN_STATUSES: tuple[str, ...] = (
    "pending",
    "validating",
    # Legacy stage names from the pre-refactor pipeline. Plans stuck
    # in these states pre-date the new pipeline and definitely can't
    # be resumed — they crash if the new background task tries.
    "chunking",
    "llm_grouping",
    "assembling",
    # New pipeline stage names. Anything mid-stage at boot is orphan.
    "partitioning",
    "subpartitioning",
    "splitting",
    "packing",
    "analyzing_concurrency",
    "annotating",
    "emitting_yaml",
)

_ORPHAN_MESSAGE = (
    "Plan generation interrupted by appliance restart; the in-process "
    "BackgroundTask did not survive. Re-create the plan to retry."
)


def fail_orphan_plans(session_factory=None) -> int:
    """Mark every in-progress plan as ``failed`` and release its VMs.

    Returns the number of plans transitioned. Idempotent: a second
    run finds nothing to do (already-failed plans are skipped).

    Designed to be called from the FastAPI lifespan once per process
    start. Uses its own short-lived session so it doesn't share state
    with the lifespan's bootstrapping logic.
    """
    factory = session_factory or _db_module.SessionLocal
    db: Session = factory()
    try:
        rows = list(
            db.scalars(
                select(MigrationPlan).where(MigrationPlan.status.in_(_ORPHAN_STATUSES))
            ).all()
        )
        if not rows:
            return 0
        now = datetime.now(timezone.utc)
        for plan in rows:
            plan.status = "failed"
            plan.error_message = _ORPHAN_MESSAGE
            plan.completed_at = now
            transition_to_available_from_failed_plan(
                plan.id,
                list(plan.vm_ids or []),
                db,
                actor="system",
            )
        db.commit()
        logger.warning(
            "startup.fail_orphan_plans count=%d ids=%s",
            len(rows),
            [p.id for p in rows],
        )
        return len(rows)
    finally:
        db.close()
