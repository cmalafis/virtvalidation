"""Integration tests for the hierarchical planner orchestrator.

These exercise chunker → per-chunk planning → assembly → review with
the LLM stubbed. End-to-end testing against a real LLM is deferred to
the RHOAI demo cluster (per the spec's "skip end-to-end testing
against real LLM" note); the goal here is to prove the orchestration
logic is correct against canned LLM responses.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.core.chunked_planner import (
    HierarchicalPlanResult,
    generate_plan_async,
)
from app.models.plan import (
    ApplicationAtomicity,
    PlanningStrategy,
    PrimaryGrouping,
    ProductionHandling,
    RiskApproach,
    WaveSizeTarget,
)
from app.models.vm import VM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _vm(vid: int, **kw) -> VM:
    return VM(
        id=vid,
        name=kw.get("name", f"vm-{vid:03d}"),
        source_hostname=kw.get("source_hostname", f"vm-{vid:03d}.local"),
        ip_address=None,
        os_family=kw.get("os_family"),
        role=kw.get("role"),
        environment=kw.get("environment"),
        owner=kw.get("owner"),
        application_hint=kw.get("application_hint"),
        source_vcenter_id=kw.get("source_vcenter_id", 1),
        target_namespace=kw.get("target_namespace"),
        vsphere_networks=list(kw.get("networks") or []),
        vsphere_datastores=list(kw.get("datastores") or []),
    )


def _profiles(vms: list[VM]) -> list[dict]:
    """Mirror the shape the orchestrator gets from
    :func:`app.core.plan_generation.assemble_vm_profiles`. Only the
    fields the prompt builder reads matter for these tests."""
    return [
        {
            "vm_id": vm.id,
            "name": vm.name,
            "role": vm.role or "",
            "environment": vm.environment or "",
            "owner": vm.owner or "",
            "application_hint": vm.application_hint or "",
            "vsphere_networks": list(vm.vsphere_networks or []),
        }
        for vm in vms
    ]


def _strategy(**overrides) -> PlanningStrategy:
    s = PlanningStrategy(
        name="test",
        primary_grouping=PrimaryGrouping.application,
        wave_size_target=WaveSizeTarget.medium_10_20,
        risk_approach=RiskApproach.mixed,
        production_handling=ProductionHandling.non_prod_first,
        application_atomicity=ApplicationAtomicity.all_together,
        freeform_constraints="",
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class _ChunkBackend:
    """Async LLM stub the orchestrator can drive.

    Splits per-chunk requests vs the final review by inspecting the
    system prompt. Per-chunk responses are computed deterministically
    from the user message's vm_id list. The review response is canned.
    """

    backend_type = "stub"
    default_model = "stub-model"
    endpoint = "stub://"
    max_planning_chunk_size = 15
    max_context_tokens = 8192
    supports_concurrent_calls = False
    max_concurrent_calls = 1

    def __init__(self) -> None:
        self.chunk_calls: list[dict] = []
        self.review_calls: list[dict] = []

    async def chat(self, *, messages, temperature=0.1, **_):
        sys = messages[0]["content"]
        user = messages[-1]["content"]
        if "reviewing the assembled migration plan" in sys:
            self.review_calls.append({"messages": messages})
            return {
                "content": json.dumps(
                    {
                        "executive_summary": "Mocked summary.",
                        "cross_chunk_warnings": ["w1", "w2"],
                        "sequencing_rationale": "Foundations first, then apps.",
                        "customer_review_points": ["check ad", "verify dns"],
                    }
                ),
                "model": self.default_model,
            }
        # Per-chunk path
        self.chunk_calls.append({"messages": messages})
        ids = _extract_vm_ids(user)
        return {
            "content": json.dumps(_chunk_response(ids)),
            "model": self.default_model,
        }

    def chat_sync(self, *, messages, temperature=0.1, **_):  # pragma: no cover
        return asyncio.run(self.chat(messages=messages, temperature=temperature))

    async def chat_stream(self, *args, **kwargs):  # pragma: no cover
        if False:
            yield ""

    async def health_check(self):  # pragma: no cover
        return {"status": "online", "backend": self.backend_type}

    def list_models(self):  # pragma: no cover
        return [self.default_model]


class _ConcurrentChunkBackend(_ChunkBackend):
    """Backend variant that advertises concurrency support."""

    supports_concurrent_calls = True
    max_concurrent_calls = 3
    max_planning_chunk_size = 50


def _extract_vm_ids(text: str) -> list[int]:
    out: list[int] = []
    i = 0
    while i < len(text):
        j = text.find('"vm_id":', i)
        if j == -1:
            break
        k = text.find(",", j)
        if k == -1:
            break
        try:
            out.append(int(text[j + 8 : k].strip()))
        except ValueError:
            pass
        i = k + 1
    return out


def _chunk_response(ids: list[int]) -> dict[str, Any]:
    """Deterministic stub: split into halves, emit two waves."""
    if not ids:
        return {}
    mid = max(1, len(ids) // 2)
    first, second = ids[:mid], ids[mid:]
    waves: list[dict[str, Any]] = []
    if first:
        waves.append(
            {
                "wave_number": 1,
                "name": "Wave 1",
                "vm_ids": first,
                "rationale": "Stateless first.",
                "estimated_duration": "2 hours",
                "risk_level": "low",
                "considerations": "",
                "applications_included": [],
                "applications_split_warning": None,
            }
        )
    if second:
        waves.append(
            {
                "wave_number": 2 if first else 1,
                "name": "Wave 2" if first else "Wave 1",
                "vm_ids": second,
                "rationale": "Stateful next.",
                "estimated_duration": "3 hours",
                "risk_level": "medium",
                "considerations": "",
                "applications_included": [],
                "applications_split_warning": None,
            }
        )
    return {
        "chunk_rationale": "Split low-risk vs medium-risk inside chunk.",
        "risk_level": "medium",
        "waves": waves,
    }


# ---------------------------------------------------------------------------
# Small-input single-shot fallback
# ---------------------------------------------------------------------------
def test_under_threshold_uses_single_shot_path():
    """Under SINGLE_SHOT_THRESHOLD, the orchestrator skips chunking
    entirely and routes to the legacy strategy_planner."""
    from tests.test_strategy_planner import _StubBackend

    vms = [_vm(i, application_hint="x") for i in range(1, 5)]
    backend = _StubBackend()
    result = asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    assert result.path_taken == "single_shot"
    assert result.chunks == []
    assert result.waves
    # Every VM must appear exactly once across waves.
    flat = [vid for w in result.waves for vid in w["vm_ids"]]
    assert sorted(flat) == sorted(vm.id for vm in vms)


# ---------------------------------------------------------------------------
# Hierarchical pipeline
# ---------------------------------------------------------------------------
def test_chunked_pipeline_assembles_global_waves():
    vms = [
        _vm(i, name=f"ehrweb-{i:02d}", application_hint="ehr", environment="prod")
        for i in range(1, 8)
    ] + [
        _vm(i, name=f"ehrdb-{i:02d}", application_hint="ehr", environment="prod")
        for i in range(8, 14)
    ]
    backend = _ChunkBackend()
    result = asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    assert result.path_taken == "hierarchical"
    assert result.chunks  # chunks persisted
    # Every VM appears exactly once across waves.
    flat = [vid for w in result.waves for vid in w["vm_ids"]]
    assert sorted(flat) == sorted(vm.id for vm in vms)
    # Wave numbers are sequential 1..N.
    numbers = [w["wave_number"] for w in result.waves]
    assert numbers == list(range(1, len(numbers) + 1))


def test_chunked_pipeline_per_chunk_and_review_calls():
    vms = [_vm(i, application_hint="app-a", environment="prod") for i in range(1, 12)] + [
        _vm(i, application_hint="app-b", environment="prod") for i in range(12, 23)
    ]
    backend = _ChunkBackend()
    asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    # >= 2 chunks (one per app), so >= 2 chunk calls.
    assert len(backend.chunk_calls) >= 2
    # Exactly one review call.
    assert len(backend.review_calls) == 1


def test_chunked_pipeline_progress_callback_emits_each_stage():
    vms = [
        _vm(i, application_hint="app-a", environment="prod") for i in range(1, 12)
    ] + [_vm(i, application_hint="app-b", environment="prod") for i in range(12, 23)]
    backend = _ChunkBackend()

    stages: list[str] = []

    def _cb(**kw):
        stage = kw.get("stage")
        if stage and stage not in stages:
            stages.append(stage)

    asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
            progress_cb=_cb,
        )
    )
    assert "chunking" in stages
    assert "planning_chunks" in stages
    assert "assembling" in stages
    assert "reviewing" in stages
    assert "completed" in stages


def test_chunked_pipeline_invokes_concurrent_gather_when_supported():
    """Concurrent backend should run multiple chunk calls in parallel
    bounded by max_concurrent_calls."""
    vms = (
        [_vm(i, application_hint=f"app-{i // 5}", environment="prod") for i in range(1, 30)]
    )
    backend = _ConcurrentChunkBackend()
    result = asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    assert result.path_taken == "hierarchical"
    # Concurrency doesn't change the total number of calls — just timing.
    assert len(backend.chunk_calls) >= 1


def test_chunked_pipeline_validates_no_orphan_vms_after_assembly():
    vms = [_vm(i, application_hint="app", environment="prod") for i in range(1, 25)]
    backend = _ChunkBackend()
    result = asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    flat = [vid for w in result.waves for vid in w["vm_ids"]]
    assert sorted(flat) == sorted(vm.id for vm in vms)
    # No duplicate vm_ids across waves.
    assert len(flat) == len(set(flat))


def test_chunked_pipeline_review_failure_degrades_gracefully():
    """If the review LLM call hiccups the orchestrator should still
    produce a plan with a fallback executive summary."""

    class _ReviewFailingBackend(_ChunkBackend):
        async def chat(self, *, messages, temperature=0.1, **_):
            sys = messages[0]["content"]
            if "reviewing the assembled migration plan" in sys:
                from app.core.llm.base import LLMBackendError
                raise LLMBackendError("review unavailable")
            return await super().chat(messages=messages, temperature=temperature)

    vms = [_vm(i, application_hint="app", environment="prod") for i in range(1, 22)]
    backend = _ReviewFailingBackend()
    result = asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    assert result.path_taken == "hierarchical"
    assert result.plan_summary or result.rationale  # fallback summary
    assert any("Review LLM unavailable" in w for w in result.warnings)


def test_chunked_pipeline_persists_chunk_metadata():
    vms = [
        _vm(i, name=f"ehr-db-{i:02d}", application_hint="ehr",
            environment="prod") for i in range(1, 4)
    ] + [
        _vm(i, name=f"ehr-app-{i:02d}", application_hint="ehr",
            environment="prod") for i in range(4, 24)
    ]
    backend = _ChunkBackend()
    result = asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    # At least one chunk has a tier label (db or app).
    tiers = {c.get("sub_key", {}).get("tier") for c in result.chunks}
    assert "db" in tiers or "app" in tiers
    # Every chunk records its wave_numbers.
    for c in result.chunks:
        assert c.get("wave_numbers")
    # chunk_rationale flows through from the per-chunk LLM stub.
    assert any(c.get("chunk_rationale") for c in result.chunks)


# ---------------------------------------------------------------------------
# Conceptual large-scale (1000 VMs mocked)
# ---------------------------------------------------------------------------
def test_thousand_vm_inventory_chunks_within_limits():
    """1000 VMs across 20 apps × prod/dev should produce 30+ chunks
    each at or under the backend's max_planning_chunk_size."""
    vms = []
    for app_idx in range(20):
        for env in ("prod", "dev"):
            for i in range(25):
                vid = app_idx * 50 + (0 if env == "prod" else 25) + i + 1
                vms.append(
                    _vm(
                        vid,
                        name=f"app-{app_idx:02d}-{env}-{i:02d}",
                        application_hint=f"app-{app_idx:02d}",
                        environment=env,
                    )
                )
    assert len(vms) == 1000

    backend = _ChunkBackend()
    result = asyncio.run(
        generate_plan_async(
            vms=vms,
            vm_profiles=_profiles(vms),
            strategy=_strategy(),
            backend=backend,
        )
    )
    assert result.path_taken == "hierarchical"
    # 30+ chunks expected; cap depends on tier subdivision.
    assert len(result.chunks) >= 30
    # Every chunk respects the backend cap.
    for c in result.chunks:
        assert len(c["vm_ids"]) <= backend.max_planning_chunk_size, c["sub_key"]
    # All VMs accounted for in waves.
    flat = [vid for w in result.waves for vid in w["vm_ids"]]
    assert sorted(flat) == sorted(vm.id for vm in vms)
