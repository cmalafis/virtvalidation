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

from app.core.llm.base import LLMBackend, LLMBackendError
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
