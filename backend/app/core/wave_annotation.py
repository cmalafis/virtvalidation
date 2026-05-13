"""Stage 6 — per-wave LLM annotation with validate-retry-fallback.

For each wave produced by Stage 4 + tagged with concurrency by Stage 5,
spawn one LLM call (parallel via ``asyncio.gather``) asking for the
structured annotation:

  - description       (operator-readable narrative)
  - risk_score        (1-5, severity)
  - risk_rationale    (one paragraph)
  - notable_concerns  (short list of bullet points)

Each call sees ≤10 groups (the wave skeleton guarantees this; we
assert it as belt-and-braces). The call is retried up to
``max_attempts`` times with the previous attempt's validation error
fed back into the next prompt. If every attempt fails validation, a
deterministic mechanical fallback produces a templated annotation —
the user always sees a valid plan, never an error from a flaky LLM.

The ``method`` field on each annotated wave surfaces which path
produced it (``llm`` / ``llm_retry_N`` / ``mechanical_fallback``)
so operators can debug regressions from the audit log.

NOTE: this module is filled in by Part 4 of the planner
rearchitecture. The Part-3 commit ships a mechanical-only
implementation so the pipeline tests can exercise stages 0-5 + 7
without an LLM. Part 4 adds the parallel LLM path on top.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# Risk heuristic constants — used by the mechanical fallback and by
# pre-LLM seeding. Values 1-5 align with the prompt schema. Tuned
# against the existing risk-tier rubric so a fallback annotation
# isn't wildly different from what the LLM would say.
RISK_RANK = {"low": 2, "medium": 3, "high": 4}


def heuristic_risk(wave) -> int:
    """Default risk score when the LLM is unavailable."""
    roles = {g.estimated_role for g in wave.groups}
    notes_lower = " ".join(g.notes for g in wave.groups).lower()
    max_rank = max((RISK_RANK.get(g.migration_risk, 3) for g in wave.groups), default=3)
    if "dc" in roles or "ldap" in notes_lower or "active directory" in notes_lower:
        return max(4, max_rank)
    if "data" in roles:
        return max(4, max_rank)
    if "infrastructure" in roles:
        return max(3, max_rank)
    if roles & {"edge", "web"}:
        return min(2, max_rank)
    return max_rank


def _mechanical_annotation(wave) -> tuple[str, int, str, list[str]]:
    """Templated description + risk score + rationale + concerns.

    Stage-4 metadata (role / state / risk / notes) is rich enough to
    produce a readable narrative without LLM. We render the same
    fields a human reviewing the plan would see if the LLM were down.
    """
    roles = sorted({g.estimated_role for g in wave.groups})
    states = sorted({g.estimated_state for g in wave.groups})
    role_label = "/".join(roles) if roles else "mixed"
    state_label = ", ".join(states) if states else "mixed"
    risk = heuristic_risk(wave)
    description = (
        f"Wave {wave.wave_number}: {wave.vm_count} VMs across "
        f"{len(wave.groups)} group(s) ({role_label} tier, {state_label}). "
        f"Aggregate cluster-level risk {wave.estimated_risk}. Cutover "
        "order follows topological dependencies + role priority; HA "
        "families are spread across waves so the original cluster keeps "
        "quorum during the migration window."
    )
    rationale = (
        f"Heuristic score {risk}/5 derived from role mix "
        f"({role_label}) and aggregate group risk ({wave.estimated_risk}). "
        "Mechanical fallback — no LLM input."
    )
    concerns: list[str] = []
    if "data" in roles:
        concerns.append("Data tier present — confirm point-in-time consistency before cutover")
    if "infrastructure" in roles or "dc" in roles:
        concerns.append("Infrastructure / DC groups — verify quorum after each wave member")
    if wave.estimated_risk == "high":
        concerns.append("Aggregate risk is high — schedule extra rollback window")
    return description, risk, rationale, concerns


async def annotate_one_wave(
    wave,
    *,
    backend,
    max_attempts: int,
    semaphore: asyncio.Semaphore | None,
):
    """Run validate-retry-fallback for a single wave. Returns an
    :class:`AnnotatedWave`.

    For the Part-3 commit this function does not call the LLM — it
    always falls back to the mechanical annotation. Part 4 wires in
    the real LLM-call loop on top of this scaffolding.
    """
    from app.core.plan_pipeline import AnnotatedWave  # avoid cycle

    description, risk, rationale, concerns = _mechanical_annotation(wave)
    return AnnotatedWave(
        wave=wave,
        description=description,
        risk_score=risk,
        risk_rationale=rationale,
        notable_concerns=concerns,
        method="mechanical_fallback",
    )


async def annotate_waves(waves, *, backend, max_attempts: int = 3):
    """Annotate every wave in parallel under the backend's concurrency limit.

    Returns a list of :class:`AnnotatedWave` in ascending wave_number order.
    """
    if not waves:
        return []
    concurrency = getattr(backend, "max_concurrent_calls", 1) if backend else 1
    semaphore = asyncio.Semaphore(max(1, concurrency))
    coros = [
        annotate_one_wave(w, backend=backend, max_attempts=max_attempts, semaphore=semaphore)
        for w in waves
    ]
    annotated = await asyncio.gather(*coros)
    return sorted(annotated, key=lambda a: a.wave.wave_number)
