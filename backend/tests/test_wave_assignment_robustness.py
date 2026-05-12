"""Robustness tests for the LLM-retry + mechanical-fallback path.

Three scenarios:

  1. LLM returns valid output on first try → method="llm", attempts=1.
  2. LLM returns invalid output once, then valid → method="llm_retry_1",
     attempts=2.
  3. LLM always returns invalid output → method="mechanical_fallback",
     attempts equals max_attempts, plan is still valid (every vm_id
     placed in exactly one wave, deterministic ordering).

The mechanical fallback is the load-bearing guarantee — federal
customers must never see a 502 from /api/plans because the LLM had
a bad day. The plan from the fallback may be less nuanced than the
LLM's, but it must be a valid plan.
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from app.core.llm.base import LLMBackend
from app.core.planner import MigrationPlanner
from app.models.vm import VM


def _vm(id_: int, name: str, **kwargs) -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.local",
        source_vcenter_id=kwargs.get("vcenter", 1),
        target_namespace=kwargs.get("target_namespace", "prod"),
        application_hint=kwargs.get("application_hint"),
        environment=kwargs.get("environment"),
        os_family=kwargs.get("os_family", "rhel"),
        role=kwargs.get("role", ""),
        vsphere_networks=list(kwargs.get("networks", [])),
        vsphere_datastores=list(kwargs.get("datastores", [])),
    )
    vm.id = id_
    return vm


class _ScriptedBackend(LLMBackend):
    """Backend that plays a pre-scripted list of responses.

    Each call pops the next entry. Entries can be either:
      - dict — returned as the LLM response (JSON-encoded into content).
      - Exception — raised to simulate transport failure.
    If the script runs out, subsequent calls reuse the LAST entry so
    "always fails" scenarios don't need an infinite list.
    """

    backend_type = "scripted"
    default_model = "scripted-mock"

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self._call_index = 0
        self.calls_made: list[str] = []

    async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
        return self._next(messages)

    def chat_sync(self, messages, model=None, temperature=0.1, max_tokens=None):
        return self._next(messages)

    def _next(self, messages):
        # Capture the user-prompt body so tests can introspect what
        # corrective context (if any) was fed back to the model.
        for m in messages:
            if m.get("role") == "user":
                self.calls_made.append(m.get("content", ""))
                break
        idx = min(self._call_index, len(self._script) - 1)
        self._call_index += 1
        entry = self._script[idx]
        if isinstance(entry, Exception):
            raise entry
        return {
            "content": json.dumps(entry),
            "model": self.default_model,
        }

    async def chat_stream(self, messages, model=None, temperature=0.1) -> AsyncIterator[str]:
        # Not exercised by the planner — yield nothing.
        if False:
            yield ""

    async def health_check(self):
        return {"status": "online", "backend": self.backend_type}

    def list_models(self):
        return [self.default_model]


def _simple_fleet() -> list[VM]:
    # Six VMs so the preclassifier's small-primary-collapse pass
    # (<5 VM threshold) doesn't merge web + db into a single group —
    # robustness tests need at least two distinct groups to exercise
    # the multi-group retry / fallback paths.
    return [
        _vm(1, "web-prod-01", networks=["w"], application_hint="app1"),
        _vm(2, "web-prod-02", networks=["w"], application_hint="app1"),
        _vm(3, "web-prod-03", networks=["w"], application_hint="app1"),
        _vm(4, "db-prod-01", networks=["d"], application_hint="app1"),
        _vm(5, "db-prod-02", networks=["d"], application_hint="app1"),
        _vm(6, "db-prod-03", networks=["d"], application_hint="app1"),
    ]


def _all_groups_in_one_wave_response(group_ids: list[str]) -> dict:
    """Valid response: every group in wave 1."""
    return {
        "summary": "single-wave plan",
        "waves": [{
            "wave_number": 1,
            "group_ids": list(group_ids),
            "rationale": "test",
            "estimated_risk": "low",
        }],
    }


def _duplicate_group_response(group_ids: list[str]) -> dict:
    """Invalid response: same group_id in two waves."""
    return {
        "summary": "bad: duplicate group_id across waves",
        "waves": [
            {
                "wave_number": 1,
                "group_ids": list(group_ids),
                "rationale": "first wave includes everything",
                "estimated_risk": "low",
            },
            {
                "wave_number": 2,
                "group_ids": list(group_ids[:1]),  # duplicate of group 1
                "rationale": "but also re-list the first group here",
                "estimated_risk": "medium",
            },
        ],
    }


def _dropped_group_response(group_ids: list[str]) -> dict:
    """Invalid response: omits a group_id entirely."""
    if not group_ids:
        return {"summary": "empty", "waves": []}
    return {
        "summary": "bad: omits the last group",
        "waves": [{
            "wave_number": 1,
            "group_ids": list(group_ids[:-1]),  # last one dropped
            "rationale": "the planner accidentally forgot one",
            "estimated_risk": "low",
        }],
    }


def _group_ids_from_prompt(prompt: str) -> list[str]:
    """Extract group ids the planner sent to the LLM."""
    import re
    return list(dict.fromkeys(re.findall(r'"id"\s*:\s*"([^"]+)"', prompt)))


# ---------------------------------------------------------------------------
# Scenario 1 — LLM returns valid output on first try
# ---------------------------------------------------------------------------
def test_llm_valid_first_attempt_method_is_llm_and_attempts_is_one():
    vms = _simple_fleet()
    # Two-pass scripted backend: the planner first calls to render a
    # prompt; the scripted backend doesn't know the group_ids ahead
    # of time, so we use a tiny pre-classifier preview to figure them
    # out and script the response.
    from app.core.preclassifier import PreClassifier
    groups = PreClassifier().classify(vms)
    group_ids = [g.id for g in groups]
    backend = _ScriptedBackend([_all_groups_in_one_wave_response(group_ids)])

    result = MigrationPlanner(backend=backend).plan_with_groups(vms)
    assert result["method"] == "llm"
    assert result["attempts"] == 1
    assert len(backend.calls_made) == 1


# ---------------------------------------------------------------------------
# Scenario 2 — LLM bad once, then valid → retry path
# ---------------------------------------------------------------------------
def test_llm_invalid_then_valid_retries_with_corrective_context():
    vms = _simple_fleet()
    from app.core.preclassifier import PreClassifier
    groups = PreClassifier().classify(vms)
    group_ids = [g.id for g in groups]

    backend = _ScriptedBackend([
        _duplicate_group_response(group_ids),
        _all_groups_in_one_wave_response(group_ids),
    ])
    result = MigrationPlanner(backend=backend).plan_with_groups(vms)
    assert result["method"] == "llm_retry_1"
    assert result["attempts"] == 2
    # Second call MUST include the corrective context — the planner
    # surfaces the previous validation error to the model so it can
    # self-correct. This is the load-bearing UX guarantee of the
    # retry loop.
    assert len(backend.calls_made) == 2
    assert "PREVIOUS ATTEMPT FAILED" in backend.calls_made[1]
    assert "multiple waves" in backend.calls_made[1].lower()


def test_llm_dropped_group_triggers_retry():
    vms = _simple_fleet()
    from app.core.preclassifier import PreClassifier
    groups = PreClassifier().classify(vms)
    group_ids = [g.id for g in groups]

    backend = _ScriptedBackend([
        _dropped_group_response(group_ids),
        _all_groups_in_one_wave_response(group_ids),
    ])
    result = MigrationPlanner(backend=backend).plan_with_groups(vms)
    assert result["method"] == "llm_retry_1"
    assert result["attempts"] == 2
    # Corrective context should mention "did not place" / missing.
    assert "did not place" in backend.calls_made[1].lower() or \
           "previous attempt failed" in backend.calls_made[1].lower()


# ---------------------------------------------------------------------------
# Scenario 3 — LLM always bad → mechanical fallback
# ---------------------------------------------------------------------------
def test_llm_always_fails_falls_back_to_mechanical_with_valid_plan():
    vms = _simple_fleet()
    from app.core.preclassifier import PreClassifier
    groups = PreClassifier().classify(vms)
    group_ids = [g.id for g in groups]

    backend = _ScriptedBackend([_duplicate_group_response(group_ids)])
    result = MigrationPlanner(backend=backend).plan_with_groups(vms)
    assert result["method"] == "mechanical_fallback"
    # 3 default attempts × always-fail → 3 calls.
    assert result["attempts"] == 3
    assert len(backend.calls_made) == 3
    # The plan must still be valid — every vm_id placed once.
    placed = sorted(vid for wave in result["waves"] for vid in wave["vm_ids"])
    assert placed == sorted(vm.id for vm in vms)
    # Mechanical summary should flag the fallback.
    assert "mechanical" in result["summary"].lower()


def test_mechanical_fallback_is_deterministic():
    vms = _simple_fleet()
    from app.core.preclassifier import PreClassifier
    groups = PreClassifier().classify(vms)
    group_ids = [g.id for g in groups]

    backend_a = _ScriptedBackend([_duplicate_group_response(group_ids)])
    backend_b = _ScriptedBackend([_duplicate_group_response(group_ids)])

    result_a = MigrationPlanner(backend=backend_a).plan_with_groups(vms)
    result_b = MigrationPlanner(backend=backend_b).plan_with_groups(vms)
    # Mechanical = deterministic. Same input → byte-identical waves.
    assert result_a["waves"] == result_b["waves"]


def test_mechanical_fallback_orders_data_before_app():
    # Data should land in an earlier wave than web per the role-
    # priority + dependency-hint rules in _topological_sort. Six VMs
    # so the small-primary-collapse (< 5 VM threshold) doesn't merge
    # everything into one group.
    vms = [
        _vm(1, "postgres-db-01", networks=["d"], application_hint="app1"),
        _vm(2, "postgres-db-02", networks=["d"], application_hint="app1"),
        _vm(3, "postgres-db-03", networks=["d"], application_hint="app1"),
        _vm(4, "web-frontend-01", networks=["w"], application_hint="app1"),
        _vm(5, "web-frontend-02", networks=["w"], application_hint="app1"),
        _vm(6, "web-frontend-03", networks=["w"], application_hint="app1"),
    ]
    from app.core.preclassifier import PreClassifier
    groups = PreClassifier().classify(vms)
    group_ids = [g.id for g in groups]
    backend = _ScriptedBackend([_duplicate_group_response(group_ids)])
    result = MigrationPlanner(backend=backend).plan_with_groups(vms)
    assert result["method"] == "mechanical_fallback"

    # Find wave that contains the data group and the wave that
    # contains the web/app group; data wave < app wave.
    wave_by_group: dict[str, int] = {}
    for wave in result["waves"]:
        for gid in wave["group_ids"]:
            wave_by_group[gid] = wave["wave_number"]
    data_waves = [w for gid, w in wave_by_group.items() if "data" in gid]
    web_waves = [w for gid, w in wave_by_group.items() if "web" in gid]
    if data_waves and web_waves:
        assert min(data_waves) < max(web_waves)


# ---------------------------------------------------------------------------
# Configurable max_attempts
# ---------------------------------------------------------------------------
def test_max_llm_attempts_one_disables_retries():
    """``max_llm_attempts=1`` → no retries, straight to mechanical on first fail."""
    vms = _simple_fleet()
    from app.core.preclassifier import PreClassifier
    groups = PreClassifier().classify(vms)
    group_ids = [g.id for g in groups]
    backend = _ScriptedBackend([_duplicate_group_response(group_ids)])
    result = MigrationPlanner(backend=backend).plan_with_groups(
        vms, max_llm_attempts=1,
    )
    assert result["method"] == "mechanical_fallback"
    assert result["attempts"] == 1
    assert len(backend.calls_made) == 1


def test_topological_sort_handles_cycles_gracefully():
    """A cycle in dependency_hints should not hang the planner."""
    from app.core.preclassifier import VMGroup, GroupKey
    from app.core.planner import MigrationPlanner

    a = VMGroup(
        key=GroupKey(vcenter_id=1, target_namespace="prod", role="app",
                     state="stateless", discriminator="x:a"),
        vm_ids=[1],
        shared_attributes={},
        estimated_role="app",
        estimated_state="stateless",
        migration_risk="medium",
        dependency_hints=["vc1/prod/app/stateless/x:b"],
        notes="A",
    )
    b = VMGroup(
        key=GroupKey(vcenter_id=1, target_namespace="prod", role="app",
                     state="stateless", discriminator="x:b"),
        vm_ids=[2],
        shared_attributes={},
        estimated_role="app",
        estimated_state="stateless",
        migration_risk="medium",
        dependency_hints=["vc1/prod/app/stateless/x:a"],  # cycle back to A
        notes="B",
    )
    ordered = MigrationPlanner._topological_sort([a, b])
    # Cycle remainder is appended in stable id order — the planner
    # doesn't refuse to plan, it just stops trying to resolve the
    # cycle. Both groups should still appear exactly once.
    assert len(ordered) == 2
    assert {g.id for g in ordered} == {a.id, b.id}
