"""Unit tests for the pieces a manual plan edit is built from.

The end-to-end behaviour is pinned in ``test_plan_edit_api.py``; these
cover the parts with their own invariants: wave rebuild, the per-call
LLM ceiling for a hand-built wave, the manual-aware deterministic text,
and the operator-context prompt.
"""

from __future__ import annotations

import asyncio

from app.core.llm.mock_backend import MockBackend
from app.core.plan_edit import classify_wave, manual_context, next_wave_number, wave_membership
from app.core.preclassifier import GroupKey, VMGroup
from app.core.wave_annotation import (
    _MAX_GROUPS_PER_WAVE,
    ManualWaveContext,
    _mechanical_annotation,
    _render_wave_prompt,
    annotate_one_wave,
    override_line,
    reduce_groups_for_annotation,
)
from app.core.wave_skeleton import MechanicalWaveAssigner, Wave, build_wave
from app.models.plan import MigrationPlan
from app.models.vm import VM


def _group(n: int, *, role: str = "app", size: int = 1, risk: str = "low") -> VMGroup:
    return VMGroup(
        key=GroupKey(
            vcenter_id=1,
            target_namespace="ns",
            role=role,
            state="stateless",
            discriminator=f"d{n:02d}",
            environment="production",
        ),
        vm_ids=list(range(n * 100, n * 100 + size)),
        shared_attributes={
            "networks": [],
            "datastores": [],
            "application_hints": [f"app{n}"],
            "environments": ["production"],
            "os_families": [],
        },
        estimated_role=role,
        estimated_state="stateless",
        migration_risk=risk,
        notes=f"role {role}",
    )


class TestBuildWave:
    def test_keeps_the_given_number(self):
        wave = build_wave(7, [_group(1, role="web"), _group(2, role="data", risk="high")])
        assert wave.wave_number == 7
        assert [g.estimated_role for g in wave.groups] == ["data", "web"]
        assert wave.estimated_risk == "high"

    def test_packer_output_is_unchanged(self):
        """_finalize now goes through build_wave; numbering stays 1..N."""
        groups = [_group(i, size=6) for i in range(1, 5)]
        waves = MechanicalWaveAssigner().assign_waves(groups)
        assert [w.wave_number for w in waves] == list(range(1, len(waves) + 1))
        assert sum(w.vm_count for w in waves) == 24


class TestWaveNumbers:
    def test_next_number_is_past_the_high_water_mark(self):
        plan = MigrationPlan(
            name="p",
            vm_ids=[1, 2],
            waves=[{"wave_number": 1, "vm_ids": [1]}, {"wave_number": 2, "vm_ids": [2]}],
            model="",
            wave_number_seq=5,  # waves 3-5 existed once and were dropped
        )
        assert wave_membership(plan) == {1: [1], 2: [2]}
        assert next_wave_number(plan) == 6

    def test_never_edited_plan_uses_current_max(self):
        plan = MigrationPlan(
            name="p", vm_ids=[1], waves=[{"wave_number": 4, "vm_ids": [1]}], model=""
        )
        assert next_wave_number(plan) == 5


class TestReduceGroups:
    def test_under_the_ceiling_is_untouched(self):
        groups = [_group(i) for i in range(1, 6)]
        assert reduce_groups_for_annotation(groups) == groups

    def test_over_the_ceiling_merges_smallest_first(self):
        groups = [_group(i, size=1) for i in range(1, 14)] + [_group(20, size=8)]
        reduced = reduce_groups_for_annotation(groups)
        assert len(reduced) == _MAX_GROUPS_PER_WAVE
        # No VM is lost or duplicated by the merge.
        assert sorted(v for g in reduced for v in g.vm_ids) == sorted(
            v for g in groups for v in g.vm_ids
        )
        # The big group was never the one merged away.
        assert any(len(g.vm_ids) == 8 for g in reduced)

    def test_hand_built_wave_view_fits_one_llm_call(self):
        vms = [
            VM(id=i, name=f"svc{i}-web-01", source_hostname="h", application_hint=f"app-{i}")
            for i in range(1, 15)
        ]
        wave, view = classify_wave(9, vms)
        assert wave.wave_number == view.wave_number == 9
        assert sorted(wave.vm_ids) == list(range(1, 15))
        assert len(view.groups) <= _MAX_GROUPS_PER_WAVE
        assert view.vm_count == 14
        # The reduced view goes through the annotator without tripping
        # its ≤10-groups assertion.
        result = asyncio.run(
            annotate_one_wave(view, backend=MockBackend(), max_attempts=1, semaphore=None)
        )
        assert result.method == "llm"


class TestManualAnnotation:
    def test_default_text_is_unchanged(self):
        wave = Wave(wave_number=2, groups=[_group(1)], estimated_risk="low")
        ann = _mechanical_annotation(wave)
        assert "HA families are spread across waves" in ann.description
        assert "Mechanical fallback" in ann.risk_rationale

    def test_manual_text_drops_packer_claims_and_lists_overrides(self):
        wave = Wave(wave_number=2, groups=[_group(1)], estimated_risk="low")
        manual = ManualWaveContext(
            actor="alice", note="batch 2 goes first", overrides=[override_line("wave_size", 12, 10)]
        )
        ann = _mechanical_annotation(wave, manual)
        assert "HA families are spread" not in ann.description
        assert "arranged this wave's membership by hand" in ann.description
        assert ann.notable_concerns[0].startswith("Operator override: Wave holds 12 VMs")
        # The operator's note is NOT interpolated into validated text: a
        # note like "batch 2" would trip the leaked-label scrub.
        assert "batch 2" not in ann.description

    def test_override_lines_name_no_family(self):
        line = override_line("ha_family_cap", 3, 2)
        assert "3 VMs of one HA family" in line and "at most 2" in line

    def test_manual_context_from_persisted_wave(self):
        assert manual_context({"wave_number": 1}) is None
        ctx = manual_context(
            {
                "manual_edit": {"edited_by": "bob", "note": "with finance"},
                "overrides": [{"code": "wave_size", "count": 11, "limit": 10}],
            }
        )
        assert ctx.actor == "bob" and ctx.note == "with finance"
        assert ctx.overrides == [override_line("wave_size", 11, 10)]


class TestOperatorContextPrompt:
    def _wave(self) -> Wave:
        return Wave(wave_number=4, groups=[_group(1)], estimated_risk="low")

    def test_absent_by_default(self):
        prompt = _render_wave_prompt(self._wave(), vm_name_by_id={})
        assert "OPERATOR CONTEXT" not in prompt

    def test_block_follows_the_wave_header(self):
        manual = ManualWaveContext(
            note="Move with Wave 9\nfinance cutover", overrides=[f"line {i}" for i in range(8)]
        )
        prompt = _render_wave_prompt(self._wave(), vm_name_by_id={}, operator_context=manual)
        assert prompt.index("=== Wave 4") < prompt.index("OPERATOR CONTEXT")
        # One line, bounded: newlines collapsed, at most 5 override lines.
        assert "Move with Wave 9 finance cutover" in prompt
        assert prompt.count("acknowledged_override:") == 5

    def test_note_is_truncated_and_scrubbed(self):
        manual = ManualWaveContext(note="prefix:ehr-db- " + "x" * 900)
        prompt = _render_wave_prompt(self._wave(), vm_name_by_id={}, operator_context=manual)
        note_line = next(line for line in prompt.splitlines() if "operator_note" in line)
        assert "prefix:" not in note_line
        assert len(note_line) < 560

    def test_mock_round_trip_with_operator_context(self):
        """The mock's wave_annotation intent must still be selected and
        still parse when the prompt carries the operator block."""
        manual = ManualWaveContext(note="Goes with Wave 9", overrides=["Wave holds 11 VMs"])
        result = asyncio.run(
            annotate_one_wave(
                self._wave(),
                backend=MockBackend(),
                max_attempts=1,
                semaphore=None,
                operator_context=manual,
            )
        )
        assert result.method == "llm"
        assert result.description
        system = result.inference_messages[0]["content"]
        assert "OPERATOR CONTEXT" in system  # the manual addendum was appended
        assert "OPERATOR CONTEXT" in result.inference_messages[1]["content"]

    def test_fallback_uses_manual_text(self):
        result = asyncio.run(
            annotate_one_wave(
                self._wave(),
                backend=None,
                max_attempts=1,
                semaphore=None,
                operator_context=ManualWaveContext(),
            )
        )
        assert result.method == "mechanical_fallback"
        assert "by hand" in result.description
