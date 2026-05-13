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
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.llm.base import LLMBackend, LLMBackendError

logger = logging.getLogger(__name__)


# Risk heuristic constants — used by the mechanical fallback and by
# pre-LLM seeding. Values 1-5 align with the prompt schema.
RISK_RANK = {"low": 2, "medium": 3, "high": 4}

# Hard ceiling on groups in any annotation call. Stage 4 already
# guarantees this (max_vms_per_wave=10, each group ≥1 VM) — we assert
# it as belt-and-braces so a future bug in the wave packer surfaces
# loudly here rather than silently inflating LLM input.
_MAX_GROUPS_PER_WAVE: int = 10


WAVE_ANNOTATION_SYSTEM_PROMPT = """You are VirtValidate, writing operator-readable
annotation for ONE migration wave. The wave structure has already been
decided by deterministic Python rules — you do NOT reassign groups,
renumber waves, or reorder anything.

You will receive one wave with at most 10 pre-formed groups. Each
group describes:
  - id (string)
  - vm_count (integer)
  - role (web | app | data | edge | infrastructure | other)
  - state (stateful | stateless | unknown)
  - risk (low | medium | high)
  - depends_on (list of group ids this group depends on)
  - notes (one-line cohesion summary)

Annotate this single wave with:

  1. description — 3-5 sentence operator-readable narrative explaining
     why these groups belong together in this wave, what cutover
     sequence works inside the wave, and key cohesion signals (shared
     networks, datastores, app boundaries).
  2. risk_score — integer 1-5 where 1 is trivial (homogeneous web
     tier, fresh dev VMs) and 5 is critical (production DC primary
     + stateful DB tier in the same wave). Calibrate against the
     group-level risk tags.
  3. risk_rationale — one paragraph explaining the score with
     reference to specific groups and concerns.
  4. notable_concerns — list of short, actionable cautions operators
     should review before applying. Empty list if the wave is
     genuinely uneventful.

Respond with a SINGLE JSON object and nothing else, matching this schema:
{
  "description": "...",
  "risk_score": 1,
  "risk_rationale": "...",
  "notable_concerns": ["..."]
}

No markdown fences, no commentary."""


class WaveAnnotation(BaseModel):
    """Pydantic validation for the Stage 6 LLM output.

    Strict: ``risk_score`` must be 1-5 inclusive; description and
    rationale must be non-empty; notable_concerns is a (possibly
    empty) list of strings. Any deviation triggers a retry with the
    error fed back into the next prompt.
    """

    description: str = Field(min_length=1, max_length=4000)
    risk_score: int = Field(ge=1, le=5)
    risk_rationale: str = Field(min_length=1, max_length=4000)
    notable_concerns: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("notable_concerns")
    @classmethod
    def _trim_concerns(cls, v: list[str]) -> list[str]:
        # Strip empty entries that some models emit when they have
        # nothing to add ("notable_concerns": [""]).
        return [c.strip() for c in v if c and c.strip()]


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


def _mechanical_annotation(wave) -> WaveAnnotation:
    """Templated annotation, no LLM. Same fields as the LLM path."""
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
    return WaveAnnotation(
        description=description,
        risk_score=risk,
        risk_rationale=rationale,
        notable_concerns=concerns,
    )


def _render_wave_prompt(wave, *, error_feedback: str | None = None) -> str:
    """Build the user message for one wave's annotation call.

    The error_feedback path is taken on retry: the prior attempt's
    Pydantic ValidationError prefaces the prompt so smaller models
    self-correct against a concrete failure signal rather than vague
    instructions.
    """
    lines: list[str] = []
    if error_feedback:
        lines.extend(
            [
                "Your previous response failed validation:",
                error_feedback,
                "Try again. Stay within the schema; do not invent fields.",
                "",
            ]
        )
    lines.append("Annotate this single wave (wave annotation schema):")
    lines.append("")
    lines.append(
        f"=== Wave {wave.wave_number} "
        f"({len(wave.groups)} group(s), aggregate risk={wave.estimated_risk}, "
        f"vm_count={wave.vm_count}) ==="
    )
    for i, g in enumerate(wave.groups, start=1):
        lines.extend(
            [
                f"  GROUP {i}: {{",
                f'    "id": "{g.key.as_string()}",',
                f'    "vm_count": {len(g.vm_ids)},',
                f'    "role": "{g.estimated_role}",',
                f'    "state": "{g.estimated_state}",',
                f'    "risk": "{g.migration_risk}",',
                f'    "depends_on": {json.dumps(list(g.dependency_hints))},',
                f'    "notes": {json.dumps(g.notes)}',
                "  }",
            ]
        )
    lines.append("")
    lines.append("Produce the JSON object specified in the system prompt now.")
    return "\n".join(lines)


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return raw


def _parse_annotation(raw: str) -> WaveAnnotation:
    """Parse + strictly validate the LLM response.

    Raises ``ValueError`` on bad JSON or schema violations; the caller
    catches and retries.
    """
    cleaned = _strip_fences(raw)
    try:
        blob: Any = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Response is not valid JSON: {exc}") from exc
    if not isinstance(blob, dict):
        raise ValueError(f"Response root must be a JSON object, got {type(blob).__name__}")
    try:
        return WaveAnnotation.model_validate(blob)
    except ValidationError as exc:
        raise ValueError(f"Schema validation failed: {exc.errors()}") from exc


async def annotate_one_wave(
    wave,
    *,
    backend: LLMBackend | None,
    max_attempts: int,
    semaphore: asyncio.Semaphore | None,
):
    """Run validate-retry-fallback for a single wave. Returns an
    :class:`AnnotatedWave` from ``plan_pipeline``.

    Path semantics, mirrored in the ``method`` field:
      - ``"llm"``                — first attempt succeeded
      - ``"llm_retry_{n}"``      — succeeded on attempt n (1-indexed)
      - ``"mechanical_fallback"``— every attempt failed or no backend
                                   was configured
    """
    from app.core.plan_pipeline import AnnotatedWave  # avoid cycle

    assert len(wave.groups) <= _MAX_GROUPS_PER_WAVE, (
        f"Wave {wave.wave_number} has {len(wave.groups)} groups; "
        f"the LLM input ceiling is {_MAX_GROUPS_PER_WAVE}"
    )

    if backend is None:
        ann = _mechanical_annotation(wave)
        return AnnotatedWave(
            wave=wave,
            description=ann.description,
            risk_score=ann.risk_score,
            risk_rationale=ann.risk_rationale,
            notable_concerns=list(ann.notable_concerns),
            method="mechanical_fallback",
        )

    error_feedback: str | None = None
    sem = semaphore or asyncio.Semaphore(1)
    for attempt in range(1, max_attempts + 1):
        prompt = _render_wave_prompt(wave, error_feedback=error_feedback)
        try:
            async with sem:
                response = await backend.chat(
                    messages=[
                        {"role": "system", "content": WAVE_ANNOTATION_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                )
        except LLMBackendError as exc:
            # Transport errors short-circuit retries — the model
            # likely isn't reachable; trying again will hit the same
            # failure. Fall through to the mechanical fallback so the
            # operator sees a usable plan.
            logger.warning(
                "wave_annotation.transport_failed wave=%d attempt=%d error=%s",
                wave.wave_number,
                attempt,
                exc,
            )
            break

        raw = response.get("content") or ""
        try:
            ann = _parse_annotation(raw)
        except ValueError as exc:
            error_feedback = str(exc)
            logger.info(
                "wave_annotation.parse_failed wave=%d attempt=%d error=%s",
                wave.wave_number,
                attempt,
                error_feedback,
            )
            continue

        method = "llm" if attempt == 1 else f"llm_retry_{attempt - 1}"
        return AnnotatedWave(
            wave=wave,
            description=ann.description,
            risk_score=ann.risk_score,
            risk_rationale=ann.risk_rationale,
            notable_concerns=list(ann.notable_concerns),
            method=method,
        )

    # Every attempt failed (or transport error). Fall back.
    logger.warning(
        "wave_annotation.fallback wave=%d attempts=%d",
        wave.wave_number,
        max_attempts,
    )
    ann = _mechanical_annotation(wave)
    return AnnotatedWave(
        wave=wave,
        description=ann.description,
        risk_score=ann.risk_score,
        risk_rationale=ann.risk_rationale,
        notable_concerns=list(ann.notable_concerns),
        method="mechanical_fallback",
    )


async def annotate_waves(waves, *, backend: LLMBackend | None, max_attempts: int = 3):
    """Annotate every wave in parallel under the backend's concurrency limit.

    Returns a list of :class:`AnnotatedWave` in ascending wave_number order.
    """
    if not waves:
        return []
    concurrency = getattr(backend, "max_concurrent_calls", 1) if backend else 1
    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
    coros = [
        annotate_one_wave(w, backend=backend, max_attempts=max_attempts, semaphore=semaphore)
        for w in waves
    ]
    annotated = await asyncio.gather(*coros)
    return sorted(annotated, key=lambda a: a.wave.wave_number)
