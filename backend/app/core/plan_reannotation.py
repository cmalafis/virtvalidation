"""Background LLM re-annotation of waves an operator rearranged.

A manual edit (``app.core.plan_edit``) commits synchronously with a
deterministic annotation — ``method="mechanical_interim"`` — so the
operator's change is never waiting on, or lost to, a model. This task
then asks the LLM to explain ONLY the touched waves as the operator
arranged them, with the operator's note and acknowledged overrides in
the prompt.

LLM Input Discipline (CLAUDE.md), for this flow:

  1. Max input per call: one wave, ≤10 groups, one operator note
     (≤500 chars) and ≤5 override lines.
  2. A hand-built wave can classify into more than 10 groups;
     ``wave_annotation.reduce_groups_for_annotation`` merges the smallest
     until it fits. The LLM never sees a larger collection.
  3. The LLM cannot change structure. A bad response fails schema
     validation → retry with feedback → mechanical text; nothing about
     wave membership depends on it.
  4. Python-only path: the interim annotation, already persisted before
     this task starts.

Write-back is guarded by a per-edit token: each edit stamps
``wave["reannotation"]["token"]``, and a result is written only if the
wave still carries the token the task was started with. A later edit —
including one that moves a VM out and back — wins.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core import db as _db_module
from app.core.audit import record_audit
from app.core.llm.inference_log import record_inference
from app.core.plan_edit import METHOD_INTERIM, classify_wave, manual_context
from app.core.wave_annotation import annotate_one_wave
from app.models.plan import MigrationPlan
from app.models.vm import VM

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def queue_wave_reannotation(plan: MigrationPlan, wave_number: int) -> str | None:
    """Stamp a fresh token on one wave so :func:`reannotate_waves_task`
    will pick it up. Returns the token, or ``None`` if the plan has no
    such wave. The caller commits and schedules the task."""
    waves = copy.deepcopy(plan.waves or [])
    for doc in waves:
        if isinstance(doc, dict) and doc.get("wave_number") == wave_number:
            token = uuid4().hex
            doc["reannotation"] = {"state": "queued", "token": token, "queued_at": _now_iso()}
            plan.waves = waves
            flag_modified(plan, "waves")
            return token
    return None


async def _annotate(jobs: list[dict[str, Any]], backend, max_attempts: int) -> list:
    concurrency = getattr(backend, "max_concurrent_calls", 1) if backend else 1
    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
    return await asyncio.gather(
        *[
            annotate_one_wave(
                job["view"],
                backend=backend,
                max_attempts=max_attempts,
                semaphore=semaphore,
                vm_name_by_id=job["names"],
                operator_context=job["manual"],
            )
            for job in jobs
        ]
    )


def reannotate_waves_task(
    plan_id: int,
    tokens: dict[int, str],
    *,
    actor: str = "system",
    max_attempts: int = 3,
) -> None:
    """BackgroundTask body: one LLM call per wave in ``tokens``.

    Never raises — a failure here must not surface as anything but a
    wave that keeps its deterministic text.
    """
    db: Session = _db_module.SessionLocal()
    try:
        plan = db.get(MigrationPlan, plan_id)
        if plan is None or plan.status != "complete":
            return
        jobs: list[dict[str, Any]] = []
        for doc in plan.waves or []:
            if not isinstance(doc, dict):
                continue
            number = doc.get("wave_number")
            state = doc.get("reannotation") or {}
            if number not in tokens or state.get("token") != tokens[number]:
                continue
            vm_ids = [int(v) for v in (doc.get("vm_ids") or [])]
            vms = list(db.scalars(select(VM).where(VM.id.in_(vm_ids))).all()) if vm_ids else []
            if not vms:
                continue
            _wave, view = classify_wave(number, vms)
            jobs.append(
                {
                    "wave_number": number,
                    "token": tokens[number],
                    "view": view,
                    "names": {vm.id: vm.name for vm in vms},
                    "manual": manual_context(doc),
                }
            )
        # End the read transaction before the model is called: LLM
        # latency must not hold a connection in a transaction.
        db.commit()
        if not jobs:
            return

        backend = None
        try:
            from app.core.llm.runtime import get_active_backend

            backend = get_active_backend()
        except Exception:  # noqa: BLE001 — no backend means deterministic text stands
            logger.exception("plan %d reannotation: no LLM backend available", plan_id)

        annotated = asyncio.run(_annotate(jobs, backend, max_attempts))

        # Write back under a row lock, only where the token still matches.
        plan = db.get(MigrationPlan, plan_id, with_for_update=True, populate_existing=True)
        if plan is None:
            return
        waves = copy.deepcopy(plan.waves or [])
        by_number = {d.get("wave_number"): d for d in waves if isinstance(d, dict)}
        method_per_wave: dict[int, str] = {}
        for job, aw in zip(jobs, annotated, strict=True):
            record_inference(
                db,
                operation="wave_annotation",
                backend_type=aw.inference_backend_type
                or getattr(backend, "backend_type", "unknown"),
                model=aw.inference_model or "",
                method=aw.method,
                input_messages=aw.inference_messages,
                output_text=aw.inference_response,
                latency_ms=aw.inference_latency_ms,
                resource_type="plan",
                resource_id=plan_id,
            )
            doc = by_number.get(job["wave_number"])
            if doc is None or (doc.get("reannotation") or {}).get("token") != job["token"]:
                continue  # edited again (or dropped) since — the newer edit owns the wave
            if aw.method.startswith("llm"):
                doc["description"] = aw.description
                doc["rationale"] = aw.description
                doc["risk_score"] = aw.risk_score
                doc["risk_rationale"] = aw.risk_rationale
                doc["notable_concerns"] = list(aw.notable_concerns)
            # A mechanical result keeps the interim text (same template)
            # and only records WHY the model's text is absent.
            doc["method"] = aw.method
            doc["reannotation"] = {
                **(doc.get("reannotation") or {}),
                "state": "done",
                "completed_at": _now_iso(),
            }
            method_per_wave[job["wave_number"]] = aw.method
        if method_per_wave:
            plan.waves = waves
            flag_modified(plan, "waves")
            record_audit(
                db,
                action="plan.edit.reannotated",
                actor=actor,
                resource_type="plan",
                resource_id=plan_id,
                details={"method_per_wave": method_per_wave},
            )
        db.commit()
    except Exception:  # noqa: BLE001 — background task; the interim text stands
        logger.exception("plan %d reannotation failed", plan_id)
        db.rollback()
    finally:
        db.close()


def abandon_stale_reannotations(session_factory=None) -> int:
    """Startup sweep: a wave still ``queued`` at boot lost its background
    task with the previous process. Mark it ``abandoned`` and call its
    text what it is — the deterministic fallback — so the UI stops
    saying an LLM rationale is pending. Returns the number of waves swept.
    """
    factory = session_factory or _db_module.SessionLocal
    db: Session = factory()
    swept = 0
    try:
        plans = db.scalars(select(MigrationPlan).where(MigrationPlan.status == "complete")).all()
        for plan in plans:
            if not any(
                isinstance(d, dict) and (d.get("reannotation") or {}).get("state") == "queued"
                for d in (plan.waves or [])
            ):
                continue
            waves = copy.deepcopy(plan.waves)
            for doc in waves:
                if not isinstance(doc, dict):
                    continue
                state = doc.get("reannotation") or {}
                if state.get("state") != "queued":
                    continue
                doc["reannotation"] = {**state, "state": "abandoned"}
                if doc.get("method") == METHOD_INTERIM:
                    doc["method"] = "mechanical_fallback"
                swept += 1
            plan.waves = waves
            flag_modified(plan, "waves")
        if swept:
            db.commit()
            logger.warning("startup.abandon_stale_reannotations waves=%d", swept)
        return swept
    finally:
        db.close()
