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
import re
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.llm.base import LLMAuthError, LLMBackend, LLMBackendError
from app.core.llm.status import clear_last_llm_error, record_last_llm_error

logger = logging.getLogger(__name__)


# Risk heuristic constants — used by the mechanical fallback and by
# pre-LLM seeding. Values 1-5 align with the prompt schema.
RISK_RANK = {"low": 2, "medium": 3, "high": 4}

# Hard ceiling on groups in any annotation call. Stage 4 already
# guarantees this (max_vms_per_wave=10, each group ≥1 VM) — we assert
# it as belt-and-braces so a future bug in the wave packer surfaces
# loudly here rather than silently inflating LLM input.
_MAX_GROUPS_PER_WAVE: int = 10


# Patterns that signal the preclassifier's internal grouping syntax
# has leaked into LLM output. We never send these labels to the model
# any more, but smaller models sometimes invent them or carry them
# from training data — the regex catches both cases and triggers a
# retry with the failing match fed back into the prompt.
_LEAKED_LABEL_RE = re.compile(
    r"batch[_ -]\d+"  # batch_2, "batch 2", batch-2
    r"|prefix:"  # prefix:<discriminator>
    r"|merged:"  # merged:<discriminator>
    r"|\bhint:"  # hint:<discriminator>
    r"|\bvc\?"  # partition fallback marker for unknown vcenter
    r"|/(?:default|prod|stg|staging|dev|qa|dr)/"
    r"(?:default|prod|stg|staging|dev|qa|dr)/",  # partition path
    re.IGNORECASE,
)


# Same pattern applied to group.notes coming OUT of the preclassifier.
# Today notes don't normally include these tokens, but a future change
# to the preclassifier could leak them — sanitizing on input is cheap
# insurance.
_NOTES_SCRUB_RE = re.compile(
    r"\b(?:batch[_ -]\d+|prefix:[^\s,;]+|merged:[^\s,;]+|hint:[^\s,;]+)",
    re.IGNORECASE,
)


WAVE_ANNOTATION_SYSTEM_PROMPT = """You are VirtValidate, writing operator-readable
annotation for ONE migration wave. The wave structure has already been
decided by deterministic Python rules — you do NOT reassign groups,
renumber waves, or reorder anything.

You will receive one wave with at most 10 pre-formed groups. Each
group describes:
  - label (string) — short operator-readable summary
  - vm_count (integer)
  - sample_vm_names (list of strings) — actual hostnames from the group
  - application (string) — operator-set application tag, or "untagged"
  - environment (string) — production | staging | development | dr | ""
  - role (web | app | data | edge | infrastructure | other)
  - state (stateful | stateless | unknown)
  - risk (low | medium | high)
  - ha_family (string, optional) — HA membership summary if applicable
  - depends_on (list of role categories this group depends on)
  - notes (one-line cohesion summary)

Annotate this single wave with:

  1. description — 3-5 sentence operator-readable narrative explaining
     what's in the wave, why these groups belong together, what
     cutover sequence works inside the wave, and key cohesion signals
     (shared networks, datastores, app boundaries). Reference VMs by
     their sample_vm_names or the application label.
  2. risk_score — integer 1-5 where 1 is trivial (homogeneous web
     tier, fresh dev VMs) and 5 is critical (production DC primary
     + stateful DB tier in the same wave). Calibrate against the
     group-level risk tags.
  3. risk_rationale — one paragraph explaining the score with
     reference to specific applications, VMs, and concerns.
  4. notable_concerns — list of short, actionable cautions operators
     should review before applying. Empty list if the wave is
     genuinely uneventful.

STRICT OUTPUT RULES — violations trigger a retry:
  - Use the application names, environments, and sample_vm_names you
    were given. Speak the way a migration engineer would.
  - Do NOT echo internal labels like "batch 2", "prefix:...",
    "merged:...", or any slash-separated partition key. Those are
    implementation details the operator does not need to see.
  - Do NOT invent VMs, applications, or environments that aren't in
    the input.

Respond with a SINGLE JSON object and nothing else, matching this schema:
{
  "description": "...",
  "risk_score": 1,
  "risk_rationale": "...",
  "notable_concerns": ["..."]
}

Example output for a wave containing a billing-app data tier and
three EHR web frontends:
{
  "description": "Wave 2 migrates the billing application's Oracle data tier (oracle-db-prod-01, oracle-db-prod-02) alongside the EHR web frontends (ehr-web-01, ehr-web-02, ehr-web-03). Drain the EHR web tier first, then quiesce billing writes before moving the database. Both groups land in the production OCP-Virt namespace on the same shared storage class.",
  "risk_score": 4,
  "risk_rationale": "Score 4: a production Oracle pair where downtime cascades to the EHR web frontend. The web tier is stateless and rolls forward easily, but the database move dominates the risk profile.",
  "notable_concerns": ["Verify Oracle SCN consistency before cutover", "Schedule an extra rollback window for the database move"]
}

No markdown fences, no commentary."""


class WaveAnnotation(BaseModel):
    """Pydantic validation for the Stage 6 LLM output.

    Strict: ``risk_score`` must be 1-5 inclusive; description and
    rationale must be non-empty; notable_concerns is a (possibly
    empty) list of strings. ``description`` and ``risk_rationale``
    must NOT contain internal partition labels (batch_N, prefix:,
    partition keys) — if they do, validation fails and the caller
    retries with the failing match fed back as feedback. Any
    deviation triggers a retry.
    """

    description: str = Field(min_length=1, max_length=4000)
    risk_score: int = Field(ge=1, le=5)
    risk_rationale: str = Field(min_length=1, max_length=4000)
    notable_concerns: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("description", "risk_rationale")
    @classmethod
    def _no_leaked_internal_labels(cls, v: str) -> str:
        match = _LEAKED_LABEL_RE.search(v)
        if match:
            raise ValueError(
                f"response leaked internal label {match.group(0)!r} — "
                "use the operator-readable application names, environments, and "
                "sample_vm_names from the input; never reference internal "
                "labels like batch numbers, prefix:..., or partition keys."
            )
        return v

    @field_validator("notable_concerns")
    @classmethod
    def _trim_concerns(cls, v: list[str]) -> list[str]:
        # Strip empty entries that some models emit when they have
        # nothing to add ("notable_concerns": [""]).
        cleaned = [c.strip() for c in v if c and c.strip()]
        # Apply the same internal-label scrub to each concern so a
        # leaked label in a bullet point also triggers retry.
        for c in cleaned:
            match = _LEAKED_LABEL_RE.search(c)
            if match:
                raise ValueError(
                    f"notable_concerns entry leaked internal label "
                    f"{match.group(0)!r} — describe the concern in "
                    "operator-readable terms (application names, hostnames, "
                    "environments) without partition keys or batch numbers."
                )
        return cleaned


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


def _operator_app_label(group) -> str:
    """Pick an operator-readable application name for a group.

    Prefers ``shared_attributes['application_hints']`` (operator-set
    tags); falls back to ``"untagged"`` so the prompt never exposes
    that the underlying field name was ``application_hint``.
    """
    hints = (group.shared_attributes or {}).get("application_hints") or []
    return hints[0] if hints and hints[0] else "untagged"


def _operator_env_label(group) -> str:
    envs = (group.shared_attributes or {}).get("environments") or []
    return envs[0] if envs and envs[0] else ""


def _ha_family_summary(group) -> str | None:
    """Human-readable HA membership summary, or None if the group is
    not part of an HA family."""
    active = [m for m in group.ha_members if m.ha_role != "standalone"]
    if len(active) < 2:
        return None
    roles = {m.ha_role for m in active}
    role_label = ", ".join(sorted(roles)) if roles else "members"
    return f"{len(active)} HA peers in this group ({role_label})"


def _role_dependency_summary(group, *, all_roles_in_wave: list[str]) -> list[str]:
    """Translate raw dependency_hints (partition keys) into role
    categories the operator recognizes.

    The preclassifier emits dependencies as a list of group IDs that
    happen to be partition keys. We don't want those keys in the LLM
    input, but the underlying *role categories* are useful signal —
    "app depends on data" survives the translation cleanly.
    """
    if group.estimated_role == "app":
        wants = {"data", "infrastructure"}
    elif group.estimated_role in {"web", "edge"}:
        wants = {"app", "data"}
    else:
        return []
    present = sorted(wants & set(all_roles_in_wave))
    return [f"{r} groups in this wave" for r in present]


def _sanitize_notes(notes: str) -> str:
    """Strip internal partition tokens from a Group.notes string
    before showing it to the LLM."""
    cleaned = _NOTES_SCRUB_RE.sub("", notes or "").strip(" ;,")
    return cleaned or "Group cohesion based on shared attributes."


def _build_group_view(group, vm_name_by_id: dict[int, str], *, wave_roles: list[str]) -> dict:
    """Sanitized per-group view for the Stage-6 prompt.

    Replaces the preclassifier's internal ``g.key.as_string()`` (a
    slash-separated partition key) with operator-readable fields
    derived from the same VMs: application label, environment, sample
    hostnames, HA family summary. Internal label tokens like
    "batch_N" and "prefix:..." cannot appear in this view's output —
    if a future change to the preclassifier leaks them into
    ``shared_attributes`` or ``notes``, ``_sanitize_notes`` strips
    them and ``_LEAKED_LABEL_RE`` on the response catches anything
    the LLM might echo back.
    """
    app_label = _operator_app_label(group)
    env_label = _operator_env_label(group)
    sample_names = [vm_name_by_id.get(vid, f"vm-{vid}") for vid in list(group.vm_ids)[:3]]
    view: dict[str, Any] = {
        "label": (
            f"{app_label} {group.estimated_role} tier "
            f"({len(group.vm_ids)} {'VM' if len(group.vm_ids) == 1 else 'VMs'})"
        ),
        "vm_count": len(group.vm_ids),
        "sample_vm_names": sample_names,
        "application": app_label,
        "environment": env_label,
        "role": group.estimated_role,
        "state": group.estimated_state,
        "risk": group.migration_risk,
        "depends_on": _role_dependency_summary(group, all_roles_in_wave=wave_roles),
        "notes": _sanitize_notes(group.notes),
    }
    ha = _ha_family_summary(group)
    if ha:
        view["ha_family"] = ha
    return view


def _render_wave_prompt(
    wave,
    *,
    vm_name_by_id: dict[int, str],
    error_feedback: str | None = None,
) -> str:
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
                "Try again. Stay within the schema; do not invent fields; "
                "do not reference internal labels.",
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
    wave_roles = [g.estimated_role for g in wave.groups]
    for i, g in enumerate(wave.groups, start=1):
        view = _build_group_view(g, vm_name_by_id, wave_roles=wave_roles)
        lines.append(f"  GROUP {i}: {json.dumps(view, indent=2)}")
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
    vm_name_by_id: dict[int, str] | None = None,
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

    name_lookup: dict[int, str] = dict(vm_name_by_id or {})

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
        prompt = _render_wave_prompt(wave, vm_name_by_id=name_lookup, error_feedback=error_feedback)
        try:
            async with sem:
                response = await backend.chat(
                    messages=[
                        {"role": "system", "content": WAVE_ANNOTATION_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                )
        except LLMAuthError:
            # Auth failures are operator-config errors (wrong/expired
            # key), not "the model had a bad day". Surface them via
            # AppSettings.last_llm_error so the Settings UI shows a
            # banner; transient failures (timeouts, 5xx) stay quiet.
            # The plan still completes via the mechanical fallback —
            # we don't block the operator on a busted key.
            logger.warning(
                "wave_annotation.auth_failed wave=%d attempt=%d backend=%s",
                wave.wave_number,
                attempt,
                getattr(backend, "backend_type", "unknown"),
            )
            record_last_llm_error(
                f"{getattr(backend, 'backend_type', 'LLM')} authentication "
                f"failed (HTTP 401/403). Wave annotations are using the "
                f"mechanical fallback. Check the configured credentials."
            )
            ann = _mechanical_annotation(wave)
            return AnnotatedWave(
                wave=wave,
                description=ann.description,
                risk_score=ann.risk_score,
                risk_rationale=ann.risk_rationale,
                notable_concerns=list(ann.notable_concerns),
                method="mechanical_fallback_auth",
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
        # Successful LLM call — clear any stale auth-failure banner.
        # Symmetric with record_last_llm_error so a fix-and-retry
        # makes the warning disappear without operator action.
        clear_last_llm_error()
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


async def annotate_waves(
    waves,
    *,
    backend: LLMBackend | None,
    max_attempts: int = 3,
    vm_name_by_id: dict[int, str] | None = None,
):
    """Annotate every wave in parallel under the backend's concurrency limit.

    ``vm_name_by_id`` plumbs operator-readable hostnames into each
    wave's prompt so the LLM can reference real names instead of
    internal vm_ids. The pipeline builds this lookup from the
    in-memory VM rows; callers without a name lookup get
    ``"vm-<id>"`` placeholders, which still avoid leaking partition
    keys.

    Returns a list of :class:`AnnotatedWave` in ascending wave_number order.
    """
    if not waves:
        return []
    concurrency = getattr(backend, "max_concurrent_calls", 1) if backend else 1
    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
    coros = [
        annotate_one_wave(
            w,
            backend=backend,
            max_attempts=max_attempts,
            semaphore=semaphore,
            vm_name_by_id=vm_name_by_id,
        )
        for w in waves
    ]
    annotated = await asyncio.gather(*coros)
    return sorted(annotated, key=lambda a: a.wave.wave_number)
