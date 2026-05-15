"""Stage 6 (per-wave LLM annotation) tests.

Pins:
  - WaveAnnotation Pydantic validation rejects bad shapes
  - annotate_one_wave succeeds on first attempt with a clean mock
  - retry path feeds the prior error back into the next prompt
  - mechanical fallback when every attempt fails OR no backend
  - method field accurately surfaces which path produced the annotation
  - asyncio.gather across multiple waves under the semaphore
  - mock backend's wave_annotation intent round-trips through the parser
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import pytest
from pydantic import ValidationError

from app.core.llm.base import LLMAuthError, LLMBackend, LLMBackendError
from app.core.llm.mock_backend import MockBackend
from app.core.preclassifier import GroupKey, VMGroup
from app.core.wave_annotation import (
    WaveAnnotation,
    _parse_annotation,
    annotate_one_wave,
    annotate_waves,
    heuristic_risk,
)
from app.core.wave_skeleton import Wave


def _group(vid_start: int = 1, role: str = "app", risk: str = "low") -> VMGroup:
    key = GroupKey(
        vcenter_id=1,
        target_namespace="ns",
        role=role,
        state="stateless",
        discriminator=f"d{vid_start}",
        environment="production",
    )
    return VMGroup(
        key=key,
        vm_ids=[vid_start, vid_start + 1],
        shared_attributes={
            "networks": [],
            "datastores": [],
            "application_hints": [],
            "environments": [],
            "os_families": [],
        },
        estimated_role=role,
        estimated_state="stateless",
        migration_risk=risk,
        dependency_hints=[],
        notes=f"role {role}",
        ha_members=[],
    )


def _wave(num: int = 1, groups=None) -> Wave:
    return Wave(wave_number=num, groups=groups or [_group()], estimated_risk="low")


class _ScriptedBackend(LLMBackend):
    """Returns responses from a queue. Raises LLMBackendError when empty."""

    backend_type = "scripted"
    default_model = "scripted"
    max_concurrent_calls = 4

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self._calls: list[list[dict]] = []

    async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
        self._calls.append(messages)
        if not self._responses:
            raise LLMBackendError("scripted backend exhausted")
        return {"content": self._responses.pop(0), "model": "scripted"}

    async def chat_stream(self, messages, model=None, temperature=0.1) -> AsyncIterator[str]:
        yield (await self.chat(messages, model=model, temperature=temperature))["content"]

    async def health_check(self):
        return {"status": "online"}

    def list_models(self):
        return ["scripted"]


class _RaisingBackend(LLMBackend):
    """Always raises LLMBackendError. Tests the transport-failure path."""

    backend_type = "raising"
    default_model = "raising"
    max_concurrent_calls = 1

    async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
        raise LLMBackendError("backend unavailable")

    async def chat_stream(self, messages, model=None, temperature=0.1) -> AsyncIterator[str]:
        raise LLMBackendError("backend unavailable")
        yield ""  # pragma: no cover

    async def health_check(self):
        return {"status": "offline"}

    def list_models(self):
        return []


_VALID_LLM_RESPONSE = json.dumps(
    {
        "description": "Wave 1 migrates two app groups co-located on tier1.",
        "risk_score": 2,
        "risk_rationale": "Low risk: stateless app tier, no DC or DB.",
        "notable_concerns": ["Confirm session affinity drains during cutover."],
    }
)


class TestWaveAnnotationSchema:
    def test_valid_payload_parses(self):
        ann = WaveAnnotation.model_validate(json.loads(_VALID_LLM_RESPONSE))
        assert ann.risk_score == 2
        assert ann.notable_concerns == ["Confirm session affinity drains during cutover."]

    def test_risk_score_must_be_1_to_5(self):
        bad = {
            "description": "x",
            "risk_score": 7,
            "risk_rationale": "x",
            "notable_concerns": [],
        }
        with pytest.raises(ValidationError):
            WaveAnnotation.model_validate(bad)

    def test_empty_description_rejected(self):
        bad = {
            "description": "",
            "risk_score": 2,
            "risk_rationale": "x",
            "notable_concerns": [],
        }
        with pytest.raises(ValidationError):
            WaveAnnotation.model_validate(bad)

    def test_empty_concerns_stripped(self):
        ann = WaveAnnotation.model_validate(
            {
                "description": "x",
                "risk_score": 2,
                "risk_rationale": "y",
                "notable_concerns": ["", "  ", "real concern"],
            }
        )
        assert ann.notable_concerns == ["real concern"]


class TestParseAnnotation:
    def test_strips_markdown_fences(self):
        wrapped = f"```json\n{_VALID_LLM_RESPONSE}\n```"
        ann = _parse_annotation(wrapped)
        assert ann.risk_score == 2

    def test_rejects_non_json(self):
        with pytest.raises(ValueError, match="valid JSON"):
            _parse_annotation("plain text response")

    def test_rejects_json_array(self):
        with pytest.raises(ValueError, match="JSON object"):
            _parse_annotation("[1,2,3]")


class TestAnnotateOneWave:
    def test_first_attempt_success(self):
        backend = _ScriptedBackend([_VALID_LLM_RESPONSE])
        result = asyncio.run(
            annotate_one_wave(_wave(), backend=backend, max_attempts=3, semaphore=None)
        )
        assert result.method == "llm"
        assert result.risk_score == 2
        assert len(backend._calls) == 1

    def test_retry_then_success(self):
        bad = "this is not json"
        backend = _ScriptedBackend([bad, _VALID_LLM_RESPONSE])
        result = asyncio.run(
            annotate_one_wave(_wave(), backend=backend, max_attempts=3, semaphore=None)
        )
        assert result.method == "llm_retry_1"
        # Second prompt carries the prior error in the user message.
        retry_user_msg = backend._calls[1][1]["content"]
        assert "Your previous response failed validation" in retry_user_msg

    def test_all_attempts_fail_falls_back(self):
        backend = _ScriptedBackend(["bad1", "bad2", "bad3"])
        result = asyncio.run(
            annotate_one_wave(_wave(), backend=backend, max_attempts=3, semaphore=None)
        )
        assert result.method == "mechanical_fallback"
        # Fallback fills out description deterministically.
        assert "Wave 1" in result.description

    def test_transport_error_falls_back_without_retry(self):
        backend = _RaisingBackend()
        result = asyncio.run(
            annotate_one_wave(_wave(), backend=backend, max_attempts=3, semaphore=None)
        )
        assert result.method == "mechanical_fallback"

    def test_no_backend_uses_mechanical_fallback(self):
        result = asyncio.run(
            annotate_one_wave(_wave(), backend=None, max_attempts=3, semaphore=None)
        )
        assert result.method == "mechanical_fallback"

    def test_auth_failure_records_status_and_uses_distinct_method(self, monkeypatch):
        """Auth failures must NOT silently fall back to a generic
        mechanical fallback — they record to ``last_llm_error`` so the
        Settings UI can show a banner. Plan still completes."""
        recorded: list[str] = []

        def _fake_record(message: str) -> None:
            recorded.append(message)

        monkeypatch.setattr("app.core.wave_annotation.record_last_llm_error", _fake_record)

        class _AuthFailingBackend(LLMBackend):
            backend_type = "maas"
            default_model = "granite"
            max_concurrent_calls = 1

            async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
                raise LLMAuthError("MaaS refused auth (HTTP 401) at https://litellm.example.com/v1")

            async def chat_stream(
                self, messages, model=None, temperature=0.1
            ) -> AsyncIterator[str]:
                raise LLMAuthError("auth")
                yield ""  # pragma: no cover

            async def health_check(self):
                return {"status": "offline"}

            def list_models(self):
                return []

        result = asyncio.run(
            annotate_one_wave(
                _wave(), backend=_AuthFailingBackend(), max_attempts=3, semaphore=None
            )
        )

        # Distinct method so audit logs distinguish auth failure from
        # transient/parse failure mechanical fallbacks.
        assert result.method == "mechanical_fallback_auth"
        # Status banner was written. Message includes backend name and
        # operator-actionable guidance.
        assert len(recorded) == 1
        assert "maas" in recorded[0].lower()
        assert "auth" in recorded[0].lower()
        # Bearer-token-shaped strings would have been scrubbed by the
        # status helper; check no obvious credential pattern leaks.
        assert "Bearer " not in recorded[0]

    def test_successful_call_clears_stale_status(self, monkeypatch):
        cleared: list[bool] = []

        def _fake_clear() -> None:
            cleared.append(True)

        monkeypatch.setattr("app.core.wave_annotation.clear_last_llm_error", _fake_clear)

        backend = _ScriptedBackend([_VALID_LLM_RESPONSE])
        result = asyncio.run(
            annotate_one_wave(_wave(), backend=backend, max_attempts=3, semaphore=None)
        )
        assert result.method == "llm"
        # Symmetric clear — fix-and-retry makes the banner go away.
        assert cleared == [True]

    def test_asserts_group_ceiling(self):
        # 11 groups would violate the per-LLM-call ceiling. The
        # assertion fires before any LLM call.
        wave = Wave(
            wave_number=1,
            groups=[_group(i * 100) for i in range(11)],
            estimated_risk="low",
        )
        with pytest.raises(AssertionError):
            asyncio.run(annotate_one_wave(wave, backend=None, max_attempts=1, semaphore=None))


class TestAnnotateWaves:
    def test_runs_in_parallel(self):
        # Three waves, each gets one LLM call; the scripted backend
        # serves them in order regardless of which coroutine arrives.
        backend = _ScriptedBackend([_VALID_LLM_RESPONSE] * 3)
        waves = [_wave(num=i) for i in range(1, 4)]
        out = asyncio.run(annotate_waves(waves, backend=backend, max_attempts=2))
        assert [a.wave.wave_number for a in out] == [1, 2, 3]
        assert all(a.method == "llm" for a in out)
        assert len(backend._calls) == 3

    def test_returns_in_wave_number_order(self):
        backend = _ScriptedBackend([_VALID_LLM_RESPONSE] * 3)
        waves = [_wave(num=2), _wave(num=1), _wave(num=3)]
        out = asyncio.run(annotate_waves(waves, backend=backend, max_attempts=1))
        assert [a.wave.wave_number for a in out] == [1, 2, 3]


class TestHeuristicRisk:
    def test_data_role_is_high(self):
        wave = _wave(groups=[_group(role="data", risk="high")])
        assert heuristic_risk(wave) >= 4

    def test_web_role_is_low(self):
        wave = _wave(groups=[_group(role="web", risk="low")])
        assert heuristic_risk(wave) <= 2


class TestMockBackendRoundTrip:
    """The mock's wave_annotation intent must produce a parseable response."""

    def test_round_trip(self):
        backend = MockBackend()
        result = asyncio.run(
            annotate_one_wave(_wave(), backend=backend, max_attempts=1, semaphore=None)
        )
        # Mock should succeed on the first attempt — proves the
        # canned schema is in lockstep with the Pydantic validator.
        assert result.method == "llm"
        assert result.description
        assert 1 <= result.risk_score <= 5


# ---------------------------------------------------------------------------
# Internal-label leakage — both directions
# ---------------------------------------------------------------------------
class TestInternalLabelScrubbing:
    """Stage 6 must never echo preclassifier partition keys to the
    operator. Validation rejects them; the prompt builder strips them
    before they reach the LLM."""

    @pytest.mark.parametrize(
        "leaked",
        [
            "Batch 2 is the priority — migrate this cluster carefully.",
            "Wave references prefix:ehr-stag-db- which spans two datastores.",
            "merged:hint:app:web+prefix:web-app contains the web tier.",
            "The vc?/default/staging/data/stateful path covers EHR staging.",
            "batch_3 is split for ESXi concurrency limits.",
        ],
    )
    def test_validator_rejects_leaked_internal_labels_in_description(self, leaked):
        with pytest.raises(ValidationError) as excinfo:
            WaveAnnotation(
                description=leaked,
                risk_score=3,
                risk_rationale="ok",
                notable_concerns=[],
            )
        assert "internal label" in str(excinfo.value)

    def test_validator_rejects_leaked_label_in_rationale(self):
        with pytest.raises(ValidationError):
            WaveAnnotation(
                description="ok",
                risk_score=3,
                risk_rationale="Risk anchored on batch_2 cohesion.",
                notable_concerns=[],
            )

    def test_validator_rejects_leaked_label_in_notable_concerns(self):
        with pytest.raises(ValidationError):
            WaveAnnotation(
                description="ok",
                risk_score=3,
                risk_rationale="ok",
                notable_concerns=["Verify batch_2 cutover order"],
            )

    def test_validator_accepts_operator_readable_output(self):
        """The example output in the system prompt must pass validation."""
        WaveAnnotation(
            description=(
                "Wave 2 migrates the billing application's Oracle data tier "
                "(oracle-db-prod-01, oracle-db-prod-02) alongside three EHR "
                "web frontends (ehr-web-01, ehr-web-02, ehr-web-03)."
            ),
            risk_score=4,
            risk_rationale=(
                "Production Oracle pair where downtime cascades to the EHR "
                "web tier. Web tier rolls forward easily; database move "
                "dominates risk."
            ),
            notable_concerns=[
                "Verify Oracle SCN consistency before cutover",
                "Schedule an extra rollback window for the database move",
            ],
        )

    def test_prompt_builder_strips_partition_keys(self):
        """The user prompt sent to the LLM must NOT contain the
        preclassifier's partition keys or batch labels."""
        from app.core.wave_annotation import _render_wave_prompt

        wave = _wave(num=2, groups=[_group(role="data", risk="high")])
        prompt = _render_wave_prompt(wave, vm_name_by_id={1: "ehr-db-01", 2: "ehr-db-02"})
        # The internal partition key would look like
        # "vc1/default/production/data/stateless/dD1" — confirm it's
        # nowhere in the prompt.
        assert "prefix:" not in prompt
        assert "batch_" not in prompt
        assert "merged:" not in prompt
        # And the operator-readable surrogate fields are present.
        assert '"label"' in prompt
        assert '"sample_vm_names"' in prompt
        assert "ehr-db-01" in prompt

    def test_retry_feedback_path_includes_validation_error(self):
        """When the LLM leaks a label, the next attempt's prompt must
        carry the validation message so smaller models self-correct."""

        async def run():
            bad = json.dumps(
                {
                    "description": "Batch 2 needs attention.",
                    "risk_score": 3,
                    "risk_rationale": "ok",
                    "notable_concerns": [],
                }
            )
            good = _VALID_LLM_RESPONSE
            backend = _ScriptedBackend([bad, good])
            result = await annotate_one_wave(
                _wave(), backend=backend, max_attempts=2, semaphore=None
            )
            return result, backend

        result, backend = asyncio.run(run())
        assert result.method == "llm_retry_1"
        # The second call's user message must mention the leaked
        # label so the model knows what to fix.
        retry_user_msg = backend._calls[1][-1]["content"]
        assert "internal label" in retry_user_msg
        assert "Batch 2" in retry_user_msg or "batch 2" in retry_user_msg.lower()
