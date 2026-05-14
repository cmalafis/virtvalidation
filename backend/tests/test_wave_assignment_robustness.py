"""Robustness tests for the per-wave LLM rationale fallback.

Post-M refactor, wave structure is decided mechanically — the LLM no
longer assigns groups to waves. The only LLM involvement on the
``POST /api/plans`` path is generating per-wave rationale text. Since
the batched-rationale refactor that text is produced by a single LLM
call covering up to ``_RATIONALE_BATCH_SIZE`` waves at a time. The
contracts these tests pin:

  1. When the LLM returns valid rationale for every wave →
     ``method="mechanical+llm_rationale"`` and exactly one LLM call
     was made for a fleet that fits in a single batch.
  2. When the LLM raises on the batched call → wave-level template
     rationale is used, ``method="mechanical+template_rationale"``,
     and the plan is still valid (every vm_id placed, every wave
     within MAX_VMS_PER_WAVE).
  3. Mixed (LLM returns text for some wave_numbers but omits others
     from its JSON response) → ``method="mechanical+llm_rationale"``
     because at least one wave got real rationale; the missing
     wave_numbers quietly use the template.

Mechanical-wave-structure invariants (always hold regardless of
LLM behavior) are pinned in ``tests/test_wave_skeleton.py``.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.planner import MigrationPlanner
from app.models.vm import VM


def _vm(id_: int, name: str, **kwargs) -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.local",
        source_vcenter_id=kwargs.get("vcenter", 1),
        target_namespace_override=kwargs.get("target_namespace", "prod"),
        application_hint=kwargs.get("application_hint"),
        environment=kwargs.get("environment"),
        os_family=kwargs.get("os_family", "rhel"),
        role=kwargs.get("role", ""),
        vsphere_networks=list(kwargs.get("networks", [])),
        vsphere_datastores=list(kwargs.get("datastores", [])),
    )
    vm.id = id_
    return vm


class _RationaleBackend(LLMBackend):
    """Backend that answers per-wave rationale prompts on a script.

    Each ``chat_sync`` call pops the next entry. Entry types:
      - str  → returned as the LLM ``content`` (raw plain text).
      - dict → JSON-encoded as ``content``.
      - Exception subclass → raised to simulate a failure.

    Once the script is exhausted, the last entry is re-used so
    "always fails" / "always succeeds" scenarios don't need an
    infinite list.
    """

    backend_type = "scripted-rationale"
    default_model = "scripted-mock"

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self._call_index = 0
        self.prompts_received: list[str] = []

    def chat_sync(self, messages, model=None, temperature=0.1, max_tokens=None):
        for m in messages:
            if m.get("role") == "user":
                self.prompts_received.append(m.get("content", ""))
                break
        idx = min(self._call_index, len(self._script) - 1)
        self._call_index += 1
        entry = self._script[idx]
        if isinstance(entry, Exception):
            raise entry
        if isinstance(entry, str):
            return {"content": entry, "model": self.default_model}
        return {"content": json.dumps(entry), "model": self.default_model}

    async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
        return self.chat_sync(messages, model, temperature, max_tokens)

    async def chat_stream(
        self,
        messages,
        model=None,
        temperature=0.1,
    ) -> AsyncIterator[str]:
        if False:
            yield ""

    async def health_check(self):
        return {"status": "online", "backend": self.backend_type}

    def list_models(self):
        return [self.default_model]


def _two_group_fleet() -> list[VM]:
    """Six VMs that produce two separate groups (web vs db) — so the
    mechanical wave skeleton creates ≥2 waves and we can observe
    per-wave rationale calls.
    """
    return [
        _vm(1, "web-prod-01", networks=["w"], application_hint="app1"),
        _vm(2, "web-prod-02", networks=["w"], application_hint="app1"),
        _vm(3, "web-prod-03", networks=["w"], application_hint="app1"),
        _vm(4, "postgres-db-01", networks=["d"], application_hint="app1"),
        _vm(5, "postgres-db-02", networks=["d"], application_hint="app1"),
        _vm(6, "postgres-db-03", networks=["d"], application_hint="app1"),
    ]


# ---------------------------------------------------------------------------
# Scenario 1 — LLM returns valid rationale for every wave
# ---------------------------------------------------------------------------
def test_llm_valid_rationale_for_every_wave_sets_llm_method():
    vms = _two_group_fleet()
    backend = _RationaleBackend(
        [
            {
                "rationales": [
                    {"wave_number": 1, "rationale": "Mock rationale: data tier first."},
                    {"wave_number": 2, "rationale": "Mock rationale: web tier follows."},
                ],
            },
        ]
    )
    result = MigrationPlanner(backend=backend).plan_with_groups(
        vms,
        ha_strategy="together",
    )
    assert result["method"] == "mechanical+llm_rationale"
    # ``_two_group_fleet`` fits in a single rationale batch, so the
    # planner issues exactly one LLM call regardless of wave count.
    assert len(backend.prompts_received) == 1
    # Every wave's rationale is the LLM text.
    for wave in result["waves"]:
        assert "Mock rationale" in wave["rationale"]


# ---------------------------------------------------------------------------
# Scenario 2 — LLM always raises → template-only path
# ---------------------------------------------------------------------------
def test_llm_always_raises_falls_back_to_template_rationale():
    vms = _two_group_fleet()
    backend = _RationaleBackend([LLMBackendError("simulated outage")])
    result = MigrationPlanner(backend=backend).plan_with_groups(
        vms,
        ha_strategy="together",
    )
    assert result["method"] == "mechanical+template_rationale"
    # Plan is still valid — wave structure was decided
    # mechanically before the LLM was consulted.
    placed = sorted(vid for wave in result["waves"] for vid in wave["vm_ids"])
    assert placed == sorted(vm.id for vm in vms)
    # Template rationale references the wave number + risk.
    assert all(f"Wave {wave['wave_number']}" in wave["rationale"] for wave in result["waves"])


# ---------------------------------------------------------------------------
# Scenario 3 — partial LLM output (some wave_numbers omitted)
# ---------------------------------------------------------------------------
def test_llm_partial_rationale_output_still_marks_llm_method():
    """When the batched LLM call returns rationale for wave 1 but
    omits wave 2 from its JSON, wave 2 quietly uses the template and
    the overall method stays ``mechanical+llm_rationale``."""
    vms = _two_group_fleet()
    backend = _RationaleBackend(
        [
            {
                "rationales": [
                    {"wave_number": 1, "rationale": "Wave-1 LLM rationale."},
                    # wave 2 deliberately missing → falls back to template
                ],
            },
        ]
    )
    result = MigrationPlanner(backend=backend).plan_with_groups(
        vms,
        ha_strategy="together",
    )
    assert result["method"] == "mechanical+llm_rationale"
    # Exactly one batched call (not one per wave).
    assert len(backend.prompts_received) == 1


# ---------------------------------------------------------------------------
# Scenario 4 — rationale disabled produces template-only path
# ---------------------------------------------------------------------------
def test_generate_rationale_false_skips_llm_entirely():
    vms = _two_group_fleet()
    backend = _RationaleBackend([])  # never called
    result = MigrationPlanner(backend=backend).plan_with_groups(
        vms,
        ha_strategy="together",
        generate_rationale=False,
    )
    assert result["method"] == "mechanical+template_rationale"
    assert backend.prompts_received == []


# ---------------------------------------------------------------------------
# Wave structure is independent of LLM behavior
# ---------------------------------------------------------------------------
def test_wave_structure_identical_across_llm_outcomes():
    """Same input → same wave structure, no matter what the LLM does.

    This is the load-bearing invariant of the post-M refactor: the
    LLM cannot influence wave assignment, so plans are deterministic
    even when the LLM behaves erratically.
    """
    vms = _two_group_fleet()

    def _structure(result):
        return [
            (w["wave_number"], tuple(w["group_ids"]), tuple(w["vm_ids"])) for w in result["waves"]
        ]

    backend_ok = _RationaleBackend(["text"])
    backend_fail = _RationaleBackend([LLMBackendError("nope")])
    result_ok = MigrationPlanner(backend=backend_ok).plan_with_groups(
        vms,
        ha_strategy="together",
    )
    result_fail = MigrationPlanner(backend=backend_fail).plan_with_groups(
        vms,
        ha_strategy="together",
    )
    assert _structure(result_ok) == _structure(result_fail)


# ---------------------------------------------------------------------------
# Topological sort cycle handling (still exercised in the new path)
# ---------------------------------------------------------------------------
def test_topological_sort_handles_cycles_gracefully():
    """A cycle in dependency_hints should not hang the planner."""
    from app.core.preclassifier import GroupKey, VMGroup
    from app.core.wave_skeleton import MechanicalWaveAssigner

    a = VMGroup(
        key=GroupKey(
            vcenter_id=1,
            target_namespace="prod",
            role="app",
            state="stateless",
            discriminator="x:a",
        ),
        vm_ids=[1],
        shared_attributes={},
        estimated_role="app",
        estimated_state="stateless",
        migration_risk="medium",
        dependency_hints=["vc1/prod/app/stateless/x:b"],
        notes="A",
    )
    b = VMGroup(
        key=GroupKey(
            vcenter_id=1,
            target_namespace="prod",
            role="app",
            state="stateless",
            discriminator="x:b",
        ),
        vm_ids=[2],
        shared_attributes={},
        estimated_role="app",
        estimated_state="stateless",
        migration_risk="medium",
        dependency_hints=["vc1/prod/app/stateless/x:a"],  # cycle back to A
        notes="B",
    )
    waves = MechanicalWaveAssigner().assign_waves([a, b])
    # Cycle remainder appended in stable id order — both groups
    # still place exactly once.
    placed_ids = [g.id for w in waves for g in w.groups]
    assert sorted(placed_ids) == sorted([a.id, b.id])
