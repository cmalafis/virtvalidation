"""Hierarchical migration planner.

Three-stage pipeline:

  1. **Chunk** (Python) — :func:`app.core.chunker.chunk_vms` partitions
     the inventory into right-sized pieces.
  2. **Per-chunk plan** (LLM, parallel where supported) — one focused
     LLM call per chunk produces wave structure + rationale for that
     chunk only.
  3. **Assemble + review** (Python + 1 LLM call) — Python aggregates
     the per-chunk plans into a single :class:`MigrationPlan`-shaped
     dict; one final LLM call reviews the chunk-level summary
     (no individual VMs) for cross-chunk concerns + an executive
     summary.

The orchestrator deliberately keeps the LLM in two narrow roles:
per-chunk reasoning at appropriate scale, and high-level review at
chunk-summary scale. Neither call ever sees the full inventory at
once, so the architecture scales from 50 to 10K+ VMs without
re-architecting prompt sizing.

Small inputs (under ``SINGLE_SHOT_THRESHOLD`` VMs) bypass chunking
entirely and route through the legacy :class:`StrategyPlanner`. The
overhead of the three-stage pipeline isn't worth it for a 5-VM run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.core.chunker import (
    SINGLE_SHOT_THRESHOLD,
    Chunk,
    chunk_vms,
    validate_chunks,
)
from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.factory import get_llm_backend
from app.core.strategy_planner import (
    SYSTEM_PROMPT as LEGACY_SYSTEM_PROMPT,
    StrategyPlanner,
    StrategyPlannerError,
    build_user_prompt as legacy_build_user_prompt,
)
from app.models.plan import PlanningStrategy
from app.models.target import ResourceMapping
from app.models.vm import VM

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-chunk system prompt — narrower than the strategy_planner prompt
# because the chunk is small and the chunker has already made the
# partitioning decisions.
# ---------------------------------------------------------------------------
PER_CHUNK_SYSTEM_PROMPT = """You are VirtValidate, a migration architect producing a wave plan for
a SINGLE chunk of a larger inventory. The chunker has already decided
which VMs belong to this chunk based on classification, target
namespace, application, and tier boundaries; your job is ONLY to
sequence these VMs into waves WITHIN the chunk.

You will receive:
  - the chunk's metadata (label, reason_for_chunk, hints)
  - a small VM list (5-50 VMs) with name, role, environment, owner,
    application_hint, vsphere_networks
  - the customer's strategy choices (wave size, risk approach,
    application atomicity)
  - the customer's freeform constraints

You MUST honor:
  - Every VM in the input lands in EXACTLY ONE wave.
  - Wave sizing matches the strategy's wave_size_target.
  - Within this chunk, dependencies are tier-based (db → app → web).
  - Do not invent vm_ids that weren't provided.

You MUST NOT:
  - Reason about VMs in other chunks (you don't see them).
  - Cross the chunker's partition boundaries (those are deliberate).

Respond with a SINGLE JSON object and nothing else. No markdown
fences. Schema:

{
  "chunk_rationale": "one paragraph explaining the wave structure for THIS chunk",
  "risk_level": "low" | "medium" | "high",
  "waves": [
    {
      "wave_number": 1,
      "name": "Wave 1: ...",
      "vm_ids": [<int>, ...],
      "rationale": "specific reason for this wave's grouping + sequencing",
      "estimated_duration": "human-readable estimate",
      "risk_level": "low" | "medium" | "high",
      "considerations": "operator notes",
      "applications_included": ["..."],
      "applications_split_warning": null
    }
  ]
}
"""


# ---------------------------------------------------------------------------
# Cross-chunk review system prompt — chunk-level summary, no VM list.
# ---------------------------------------------------------------------------
REVIEW_SYSTEM_PROMPT = """You are VirtValidate, reviewing the assembled migration plan at the
chunk level. You will see a summary of every chunk (its label, VM
count, applications, wave count, risk level) plus the chunker's
sequencing decisions — but you will NOT see individual VMs.

Your job:
  1. Write a 2-3 paragraph executive summary of the migration plan.
  2. Identify cross-chunk concerns the per-chunk planner could not
     have caught (e.g., "EHR application tier in chunk-3 depends on
     EHR database in chunk-7, but chunk-7 is sequenced later").
  3. Suggest customer review points the operator should validate
     before approving the plan for execution.

Respond with a SINGLE JSON object and nothing else. No markdown
fences. Schema:

{
  "executive_summary": "2-3 paragraphs",
  "cross_chunk_warnings": ["...", "..."],
  "sequencing_rationale": "one paragraph on the inter-chunk ordering",
  "customer_review_points": ["...", "..."]
}
"""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
@dataclass
class ChunkPlan:
    """Per-chunk LLM output, parsed + validated."""

    chunk_id: str
    label: str
    rationale: str
    risk_level: str
    waves: list[dict]
    vm_ids: list[int]


@dataclass
class HierarchicalPlanResult:
    """Final orchestrator output. Shaped to drop into the existing
    :class:`MigrationPlan` row's ``waves`` / ``rationale`` /
    ``warnings`` / ``next_actions`` columns without translation."""

    plan_summary: str
    rationale: str
    warnings: list[str]
    next_actions: list[str]
    waves: list[dict]
    chunks: list[dict]  # serialized chunks with chunk_rationale + wave_numbers
    model: str
    generation_prompt: str = ""
    generation_response: str = ""
    path_taken: str = "hierarchical"  # "hierarchical" | "single_shot"


# ---------------------------------------------------------------------------
# Progress callback shape
# ---------------------------------------------------------------------------
ProgressCallback = Callable[..., None]


# ---------------------------------------------------------------------------
# Per-chunk LLM call
# ---------------------------------------------------------------------------
async def _generate_plan_for_chunk(
    chunk: Chunk,
    chunk_vm_profiles: list[dict],
    strategy: PlanningStrategy,
    backend: LLMBackend,
) -> ChunkPlan:
    """Run one focused LLM call for a single chunk."""
    user_msg = _build_chunk_user_prompt(chunk, chunk_vm_profiles, strategy)
    try:
        response = await backend.chat(
            messages=[
                {"role": "system", "content": PER_CHUNK_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )
    except LLMBackendError as e:
        raise StrategyPlannerError(
            f"LLM backend failure on chunk {chunk.chunk_id}: {e}"
        ) from e
    raw = response.get("content", "")
    if not raw:
        raise StrategyPlannerError(
            f"Chunk {chunk.chunk_id}: LLM returned empty content"
        )
    return _parse_chunk_response(chunk, raw)


def _build_chunk_user_prompt(
    chunk: Chunk,
    chunk_vm_profiles: list[dict],
    strategy: PlanningStrategy,
) -> str:
    chunk_meta = {
        "chunk_id": chunk.chunk_id,
        "label": chunk.sub_key.get("label") or "unlabeled",
        "reason_for_chunk": chunk.reason_for_chunk,
        "is_foundation": bool(chunk.sub_key.get("is_foundation")),
        "partition": chunk.partition_key,
        "sub_key": {k: v for k, v in chunk.sub_key.items() if k != "label"},
        "hints": chunk.hints,
    }
    return (
        "## Chunk metadata\n\n"
        f"```json\n{json.dumps(chunk_meta, indent=2, default=str)}\n```\n\n"
        f"## Customer strategy + constraints\n\n"
        f"- Wave size target: {strategy.wave_size_target.value}\n"
        f"- Risk approach: {strategy.risk_approach.value}\n"
        f"- Production handling: {strategy.production_handling.value}\n"
        f"- Application atomicity: {strategy.application_atomicity.value}\n"
        f"- Freeform constraints:\n{strategy.freeform_constraints or '(none)'}\n\n"
        f"## VM inventory in this chunk ({len(chunk_vm_profiles)} VMs)\n\n"
        f"```json\n{json.dumps(chunk_vm_profiles, indent=2, sort_keys=True, default=str)}\n```\n\n"
        f"Produce the JSON object specified in the system prompt now."
    )


_ALLOWED_RISK = {"low", "medium", "high"}


def _parse_chunk_response(chunk: Chunk, raw: str) -> ChunkPlan:
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as e:
        raise StrategyPlannerError(
            f"Chunk {chunk.chunk_id}: LLM output not JSON: {e}"
        ) from e
    if not isinstance(body, dict):
        raise StrategyPlannerError(
            f"Chunk {chunk.chunk_id}: LLM output not a JSON object"
        )

    waves_raw = body.get("waves") or []
    if not isinstance(waves_raw, list) or not waves_raw:
        raise StrategyPlannerError(
            f"Chunk {chunk.chunk_id}: 'waves' must be a non-empty list"
        )

    expected_ids = set(chunk.vm_ids)
    seen_ids: set[int] = set()
    waves: list[dict] = []
    for idx, w in enumerate(waves_raw, start=1):
        if not isinstance(w, dict):
            raise StrategyPlannerError(
                f"Chunk {chunk.chunk_id}: wave {idx} not an object"
            )
        wn = w.get("wave_number", idx)
        ids_raw = w.get("vm_ids") or []
        if not isinstance(ids_raw, list) or not all(isinstance(v, int) for v in ids_raw):
            raise StrategyPlannerError(
                f"Chunk {chunk.chunk_id} wave {wn}: vm_ids must be list[int]"
            )
        unknown = set(ids_raw) - expected_ids
        if unknown:
            raise StrategyPlannerError(
                f"Chunk {chunk.chunk_id} wave {wn}: unknown vm_ids {sorted(unknown)}"
            )
        dupes = seen_ids & set(ids_raw)
        if dupes:
            raise StrategyPlannerError(
                f"Chunk {chunk.chunk_id} wave {wn}: duplicate vm_ids {sorted(dupes)}"
            )
        seen_ids.update(ids_raw)
        risk = (w.get("risk_level") or "medium").lower()
        if risk not in _ALLOWED_RISK:
            risk = "medium"
        rationale = (w.get("rationale") or "").strip()
        waves.append(
            {
                "wave_number": int(wn),
                "name": (w.get("name") or f"Wave {wn}").strip(),
                "vm_ids": list(ids_raw),
                "rationale": rationale or "(no rationale)",
                "estimated_duration": (w.get("estimated_duration") or "").strip(),
                "risk_level": risk,
                "estimated_risk": risk,
                "considerations": (w.get("considerations") or "").strip(),
                "applications_included": list(w.get("applications_included") or []),
                "applications_split_warning": w.get("applications_split_warning") or None,
                "chunk_id": chunk.chunk_id,
            }
        )
    missing = expected_ids - seen_ids
    if missing:
        raise StrategyPlannerError(
            f"Chunk {chunk.chunk_id}: planner missed vm_ids {sorted(missing)}"
        )

    chunk_risk = (body.get("risk_level") or "medium").lower()
    if chunk_risk not in _ALLOWED_RISK:
        chunk_risk = "medium"

    return ChunkPlan(
        chunk_id=chunk.chunk_id,
        label=chunk.sub_key.get("label") or "unlabeled",
        rationale=(body.get("chunk_rationale") or "").strip(),
        risk_level=chunk_risk,
        waves=waves,
        vm_ids=list(chunk.vm_ids),
    )


# ---------------------------------------------------------------------------
# Cross-chunk review LLM call
# ---------------------------------------------------------------------------
async def _run_review(
    chunk_plans: list[ChunkPlan],
    chunks: list[Chunk],
    strategy: PlanningStrategy,
    backend: LLMBackend,
) -> dict:
    summary = {
        "total_vms": sum(len(cp.vm_ids) for cp in chunk_plans),
        "chunk_count": len(chunk_plans),
        "chunks": [
            {
                "id": cp.chunk_id,
                "label": cp.label,
                "vm_count": len(cp.vm_ids),
                "wave_count": len(cp.waves),
                "risk_level": cp.risk_level,
                "applications": _applications_in_chunk(cp),
                "is_foundation": _is_foundation_chunk(chunks, cp.chunk_id),
            }
            for cp in chunk_plans
        ],
        "sequencing_decisions": _summarize_dependencies(chunks),
        "strategy": {
            "wave_size_target": strategy.wave_size_target.value,
            "risk_approach": strategy.risk_approach.value,
            "production_handling": strategy.production_handling.value,
            "application_atomicity": strategy.application_atomicity.value,
            "freeform_constraints": strategy.freeform_constraints or "",
        },
    }
    user_msg = (
        "## Plan summary\n\n"
        f"```json\n{json.dumps(summary, indent=2)}\n```\n\n"
        "Produce the JSON object specified in the system prompt now."
    )
    try:
        response = await backend.chat(
            messages=[
                {"role": "system", "content": REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
        )
    except LLMBackendError as e:
        # Review is a nice-to-have; don't fail the whole plan if the
        # final call hiccups. Surface the error as a warning instead.
        logger.warning("Final review LLM call failed: %s", e)
        return {
            "executive_summary": (
                f"Plan covers {summary['total_vms']} VMs across "
                f"{summary['chunk_count']} chunks. Review LLM call failed; "
                "operator should manually review the per-chunk rationales."
            ),
            "cross_chunk_warnings": [f"Review LLM unavailable: {e}"],
            "sequencing_rationale": "",
            "customer_review_points": [],
        }
    raw = response.get("content", "")
    try:
        body = json.loads(raw or "{}")
    except json.JSONDecodeError:
        logger.warning("Final review returned invalid JSON: %r", raw[:200])
        body = {}
    return {
        "executive_summary": (body.get("executive_summary") or "").strip()
        or _default_executive_summary(summary),
        "cross_chunk_warnings": list(body.get("cross_chunk_warnings") or []),
        "sequencing_rationale": (body.get("sequencing_rationale") or "").strip(),
        "customer_review_points": list(body.get("customer_review_points") or []),
    }


def _applications_in_chunk(cp: ChunkPlan) -> list[str]:
    apps: set[str] = set()
    for w in cp.waves:
        for a in w.get("applications_included") or []:
            apps.add(a)
    return sorted(apps)


def _is_foundation_chunk(chunks: list[Chunk], chunk_id: str) -> bool:
    for c in chunks:
        if c.chunk_id == chunk_id:
            return bool(c.sub_key.get("is_foundation"))
    return False


def _summarize_dependencies(chunks: list[Chunk]) -> list[dict]:
    out: list[dict] = []
    by_id = {c.chunk_id: c for c in chunks}
    for c in chunks:
        if not c.sequence_dependencies:
            continue
        out.append(
            {
                "chunk": c.sub_key.get("label") or c.chunk_id,
                "depends_on": [
                    by_id[d].sub_key.get("label") or d
                    for d in c.sequence_dependencies
                    if d in by_id
                ],
            }
        )
    return out


def _default_executive_summary(summary: dict) -> str:
    return (
        f"Migration plan covers {summary['total_vms']} VMs across "
        f"{summary['chunk_count']} chunks. The chunker partitioned "
        "the inventory by classification, target namespace, and "
        "application boundaries; each chunk's waves were planned "
        "independently."
    )


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def _assemble_master_plan(
    chunks: list[Chunk],
    chunk_plans: list[ChunkPlan],
    review: dict,
    backend: LLMBackend,
) -> HierarchicalPlanResult:
    """Stitch per-chunk waves into one global wave list.

    Ordering is determined by the chunker's pre-computed
    ``_ordering_key`` (foundation → partition → tier), so we just walk
    chunks in the order the chunker returned them and renumber waves
    sequentially across the assembled list.
    """
    global_waves: list[dict] = []
    chunk_by_id = {c.chunk_id: c for c in chunks}
    chunk_summaries: list[dict] = []
    next_wave_num = 1

    cp_by_id = {cp.chunk_id: cp for cp in chunk_plans}

    for chunk in chunks:
        cp = cp_by_id.get(chunk.chunk_id)
        if cp is None:
            continue
        wave_numbers_in_chunk: list[int] = []
        # Local waves are 1-indexed inside their chunk; shift to global.
        local_sorted = sorted(cp.waves, key=lambda w: w["wave_number"])
        for w in local_sorted:
            shifted = dict(w)
            shifted["wave_number"] = next_wave_num
            shifted["chunk_id"] = chunk.chunk_id
            shifted["chunk_label"] = chunk.sub_key.get("label") or "unlabeled"
            global_waves.append(shifted)
            wave_numbers_in_chunk.append(next_wave_num)
            next_wave_num += 1

        chunk_summaries.append(
            {
                **chunk.to_dict(),
                "label": chunk.sub_key.get("label") or "unlabeled",
                "chunk_rationale": cp.rationale,
                "chunk_risk_level": cp.risk_level,
                "wave_numbers": wave_numbers_in_chunk,
            }
        )

    warnings = list(review.get("cross_chunk_warnings") or [])
    next_actions = list(review.get("customer_review_points") or [])
    rationale = (
        review.get("sequencing_rationale")
        or _default_executive_summary(
            {
                "total_vms": sum(len(cp.vm_ids) for cp in chunk_plans),
                "chunk_count": len(chunk_plans),
            }
        )
    )

    return HierarchicalPlanResult(
        plan_summary=review.get("executive_summary") or "",
        rationale=rationale,
        warnings=warnings,
        next_actions=next_actions,
        waves=global_waves,
        chunks=chunk_summaries,
        model=backend.default_model or "",
        path_taken="hierarchical",
    )


def _validate_assembled_plan(
    result: HierarchicalPlanResult, expected_vm_ids: set[int]
) -> None:
    """Final integrity check before returning to the caller.

    The chunker already validated chunks; the per-chunk parser
    validated each chunk's waves. This double-checks the assembled
    list — defensive belt-and-suspenders since federal customers
    take wave assignments at face value.
    """
    seen: dict[int, int] = {}  # vm_id → wave_number
    for w in result.waves:
        for vm_id in w["vm_ids"]:
            if vm_id in seen:
                raise StrategyPlannerError(
                    f"vm_id {vm_id} appears in waves {seen[vm_id]} and "
                    f"{w['wave_number']} of the assembled plan"
                )
            seen[vm_id] = w["wave_number"]

    missing = expected_vm_ids - set(seen)
    if missing:
        raise StrategyPlannerError(
            f"Assembled plan misses vm_ids {sorted(missing)[:10]}"
        )
    extras = set(seen) - expected_vm_ids
    if extras:
        raise StrategyPlannerError(
            f"Assembled plan contains unknown vm_ids {sorted(extras)[:10]}"
        )

    numbers = [w["wave_number"] for w in result.waves]
    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        raise StrategyPlannerError(
            f"Wave numbers are not sequential: {numbers}"
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
async def generate_plan_async(
    *,
    vms: list[VM],
    vm_profiles: list[dict],
    strategy: PlanningStrategy,
    mappings: Optional[ResourceMapping] = None,
    classification_by_vcenter: Optional[dict[int, str]] = None,
    backend: Optional[LLMBackend] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> HierarchicalPlanResult:
    """Three-stage hierarchical plan.

    For ``len(vms) < SINGLE_SHOT_THRESHOLD`` falls back to the
    single-shot :class:`StrategyPlanner` because the chunker /
    per-chunk overhead isn't worth it on a tiny scope.
    """
    backend = backend or get_llm_backend()
    expected_ids = {p["vm_id"] for p in vm_profiles}
    started = time.monotonic()

    # Small-input fallback — use the existing single-shot planner.
    if len(vms) < SINGLE_SHOT_THRESHOLD:
        if progress_cb:
            progress_cb(stage="planning_single_shot", chunks_total=1, chunks_complete=0)
        logger.info(
            "Hierarchical planner using single_shot path (vm_count=%d, threshold=%d)",
            len(vms),
            SINGLE_SHOT_THRESHOLD,
        )
        legacy = StrategyPlanner(backend=backend)
        # StrategyPlanner.plan() is sync — wrap with to_thread.
        plan_dict = await asyncio.to_thread(legacy.plan, strategy, vm_profiles)
        if progress_cb:
            progress_cb(
                stage="completed",
                chunks_total=1,
                chunks_complete=1,
                elapsed_seconds=int(time.monotonic() - started),
            )
        return HierarchicalPlanResult(
            plan_summary=plan_dict.get("plan_summary") or "",
            rationale=plan_dict.get("rationale") or "",
            warnings=list(plan_dict.get("warnings") or []),
            next_actions=list(plan_dict.get("next_actions") or []),
            waves=list(plan_dict.get("waves") or []),
            chunks=[],  # single_shot has no chunks
            model=plan_dict.get("model") or backend.default_model or "",
            generation_prompt=plan_dict.get("generation_prompt") or "",
            generation_response=plan_dict.get("generation_response") or "",
            path_taken="single_shot",
        )

    # Stage 1: chunk
    if progress_cb:
        progress_cb(stage="chunking", chunks_total=0, chunks_complete=0)
    chunks = chunk_vms(
        vms,
        mappings=mappings,
        strategy=strategy,
        max_size=backend.max_planning_chunk_size,
        classification_by_vcenter=classification_by_vcenter,
    )
    validate_chunks(chunks, expected_ids)
    if not chunks:
        raise StrategyPlannerError("Chunker produced zero chunks")

    profiles_by_id = {p["vm_id"]: p for p in vm_profiles}

    # Stage 2: per-chunk planning
    if progress_cb:
        progress_cb(
            stage="planning_chunks",
            chunks_total=len(chunks),
            chunks_complete=0,
        )

    async def _run_one(chunk: Chunk, idx: int) -> ChunkPlan:
        if progress_cb:
            progress_cb(
                stage="planning_chunks",
                chunks_total=len(chunks),
                chunks_complete=idx,
                current_chunk=chunk.sub_key.get("label") or chunk.chunk_id,
                elapsed_seconds=int(time.monotonic() - started),
            )
        chunk_profiles = [profiles_by_id[vid] for vid in chunk.vm_ids if vid in profiles_by_id]
        result = await _generate_plan_for_chunk(chunk, chunk_profiles, strategy, backend)
        if progress_cb:
            progress_cb(
                stage="planning_chunks",
                chunks_total=len(chunks),
                chunks_complete=idx + 1,
                current_chunk=chunk.sub_key.get("label") or chunk.chunk_id,
                elapsed_seconds=int(time.monotonic() - started),
            )
        return result

    chunk_plans: list[ChunkPlan]
    if backend.supports_concurrent_calls and backend.max_concurrent_calls > 1:
        chunk_plans = await _gather_with_limit(
            [_run_one(c, i) for i, c in enumerate(chunks)],
            limit=backend.max_concurrent_calls,
        )
    else:
        chunk_plans = []
        for i, c in enumerate(chunks):
            chunk_plans.append(await _run_one(c, i))

    # Stage 3: assemble + review
    if progress_cb:
        progress_cb(
            stage="assembling",
            chunks_total=len(chunks),
            chunks_complete=len(chunks),
            elapsed_seconds=int(time.monotonic() - started),
        )

    if progress_cb:
        progress_cb(
            stage="reviewing",
            chunks_total=len(chunks),
            chunks_complete=len(chunks),
            elapsed_seconds=int(time.monotonic() - started),
        )
    review = await _run_review(chunk_plans, chunks, strategy, backend)

    result = _assemble_master_plan(chunks, chunk_plans, review, backend)
    _validate_assembled_plan(result, expected_ids)

    if progress_cb:
        progress_cb(
            stage="completed",
            chunks_total=len(chunks),
            chunks_complete=len(chunks),
            elapsed_seconds=int(time.monotonic() - started),
        )
    return result


def generate_plan(
    *,
    vms: list[VM],
    vm_profiles: list[dict],
    strategy: PlanningStrategy,
    mappings: Optional[ResourceMapping] = None,
    classification_by_vcenter: Optional[dict[int, str]] = None,
    backend: Optional[LLMBackend] = None,
    progress_cb: Optional[ProgressCallback] = None,
) -> HierarchicalPlanResult:
    """Sync entry point for FastAPI BackgroundTasks. Wraps the async
    pipeline in :func:`asyncio.run`."""
    return asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=vm_profiles,
            strategy=strategy,
            mappings=mappings,
            classification_by_vcenter=classification_by_vcenter,
            backend=backend,
            progress_cb=progress_cb,
        )
    )


async def _gather_with_limit(
    aws: list[Awaitable], *, limit: int
) -> list:
    """Run awaitables with a concurrency limit. Preserves input order."""
    sem = asyncio.Semaphore(max(1, limit))

    async def _wrap(idx: int, aw: Awaitable):
        async with sem:
            return idx, await aw

    results = await asyncio.gather(*(_wrap(i, a) for i, a in enumerate(aws)))
    return [r for _, r in sorted(results, key=lambda x: x[0])]


# Re-export the legacy single-shot symbols so callers that still
# import them from strategy_planner keep working — and so the
# fallback path in this module has access to them.
__all__ = [
    "ChunkPlan",
    "HierarchicalPlanResult",
    "generate_plan",
    "generate_plan_async",
    "LEGACY_SYSTEM_PROMPT",
    "legacy_build_user_prompt",
]
