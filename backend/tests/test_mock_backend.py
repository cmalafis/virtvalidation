"""Tests for the mock LLM backend.

Pins three things:
  1. The factory honors ``LLM_BACKEND_TYPE=mock``.
  2. Canned responses match the JSON schema each downstream parser
     expects (categorizer, planner, validator) — if these drift, the
     orchestrators silently break in mock-mode dev runs.
  3. The "fast and deterministic" contract — same prompt in, same
     JSON out, all within 100ms even on the largest inputs we hit in
     tests.
"""

from __future__ import annotations

import json
import time

import pytest

from app.core.llm.base import LLMBackend
from app.core.llm.factory import get_llm_backend, reset_backend_cache
from app.core.llm.mock_backend import MockBackend


# ---------------------------------------------------------------------------
# Speed contract
# ---------------------------------------------------------------------------
def test_chat_sync_returns_in_under_100ms():
    backend = MockBackend()
    # Build a 1000-VM categorization prompt — the upper end of what the
    # real orchestrator passes through the planner chunker.
    payload = {"VMs": [{"vm_id": i, "name": f"vm-{i:04d}", "role": "app"} for i in range(1000)]}
    messages = [
        {"role": "system", "content": "Level 1 categorizer"},
        {"role": "user", "content": json.dumps(payload)},
    ]
    started = time.monotonic()
    result = backend.chat_sync(messages)
    elapsed_ms = (time.monotonic() - started) * 1000
    assert elapsed_ms < 100, f"chat_sync took {elapsed_ms:.1f}ms; expected <100"
    assert isinstance(result, dict)
    assert "content" in result


def test_chat_sync_deterministic():
    backend = MockBackend()
    messages = [
        {"role": "system", "content": "Wave plan generator"},
        {"role": "user", "content": json.dumps({"vm_id": 1, "name": "foo"})},
    ]
    first = backend.chat_sync(messages)["content"]
    second = backend.chat_sync(messages)["content"]
    assert first == second


# ---------------------------------------------------------------------------
# Schema contracts — downstream parsers must accept the canned output
# ---------------------------------------------------------------------------
def test_categorization_response_matches_parser_schema():
    backend = MockBackend()
    user_payload = {
        "VMs": [
            {"vm_id": 7, "name": "db-prod-01"},
            {"vm_id": 8, "name": "db-prod-02"},
        ]
    }
    messages = [
        {"role": "system", "content": "Level 1 categorizer prompt"},
        {"role": "user", "content": json.dumps(user_payload)},
    ]
    parsed = json.loads(backend.chat_sync(messages)["content"])

    groups = parsed["groups"]
    kinds = {g["kind"] for g in groups}
    # The real categorizer requires every input vm to appear in one
    # application, one environment, and one business_unit group.
    assert {"application", "environment", "business_unit"} <= kinds
    for g in groups:
        assert isinstance(g["name"], str) and g["name"]
        assert isinstance(g["description"], str)
        member_ids = {m["vm_id"] for m in g["members"]}
        assert {7, 8} <= member_ids
        for m in g["members"]:
            assert 0.0 <= m["confidence"] <= 1.0
            assert isinstance(m["rationale"], str)


def test_planning_response_matches_parser_schema():
    backend = MockBackend()
    user_payload = {
        "VMs to plan": [
            {"vm_id": 1, "name": "db-01"},
            {"vm_id": 2, "name": "app-01"},
            {"vm_id": 3, "name": "app-02"},
        ]
    }
    messages = [
        {"role": "system", "content": "Migration plan wave_number generator"},
        {"role": "user", "content": json.dumps(user_payload)},
    ]
    parsed = json.loads(backend.chat_sync(messages)["content"])

    assert "summary" in parsed
    waves = parsed["waves"]
    assert isinstance(waves, list) and waves
    # Every input vm_id appears in exactly one wave.
    all_ids: list[int] = []
    for w in waves:
        assert isinstance(w["wave_number"], int)
        assert w["estimated_risk"] in {"low", "medium", "high"}
        assert isinstance(w["rationale"], str)
        all_ids.extend(w["vm_ids"])
    assert sorted(all_ids) == [1, 2, 3]
    assert len(all_ids) == len(set(all_ids)), "vm_id duplicated across waves"


def test_planning_response_single_vm_uses_single_wave():
    backend = MockBackend()
    messages = [
        {"role": "system", "content": "Migration plan wave_number generator"},
        {"role": "user", "content": json.dumps({"vm_id": 42, "name": "lonely"})},
    ]
    parsed = json.loads(backend.chat_sync(messages)["content"])
    assert len(parsed["waves"]) == 1
    assert parsed["waves"][0]["vm_ids"] == [42]


def test_validation_response_matches_parser_schema():
    backend = MockBackend()
    messages = [
        {"role": "system", "content": "Post-migration verdict — diff vs baseline"},
        {"role": "user", "content": "baseline + current state diff"},
    ]
    parsed = json.loads(backend.chat_sync(messages)["content"])
    assert parsed["status"] in {"pass", "warn", "fail"}
    assert isinstance(parsed["summary"], str) and parsed["summary"]
    assert isinstance(parsed["findings"], list)
    assert isinstance(parsed["remediation"], list)


def test_topology_response_shape():
    backend = MockBackend()
    messages = [
        {"role": "system", "content": "Topology detection — load_balanced clusters"},
        {"role": "user", "content": json.dumps({"vm_id": 1, "name": "lb-01"})},
    ]
    parsed = json.loads(backend.chat_sync(messages)["content"])
    patterns = parsed["detected_patterns"]
    assert isinstance(patterns, list) and patterns
    p = patterns[0]
    assert "type" in p and "members" in p and "confidence" in p
    assert isinstance(p["members"], list) and p["members"]
    assert "vm_id" in p["members"][0]


def test_generic_intent_fallback():
    backend = MockBackend()
    messages = [{"role": "user", "content": "what is the capital of france"}]
    parsed = json.loads(backend.chat_sync(messages)["content"])
    # Generic fallback returns a permissive shape; it should at minimum
    # be a JSON object.
    assert isinstance(parsed, dict)


# ---------------------------------------------------------------------------
# Factory wiring
# ---------------------------------------------------------------------------
def test_factory_returns_mock_backend_for_mock_type(monkeypatch):
    from app.core import config as cfg_mod

    monkeypatch.setattr(cfg_mod.settings, "llm_backend_type", "mock")
    reset_backend_cache()
    try:
        backend = get_llm_backend()
        assert isinstance(backend, MockBackend)
        assert isinstance(backend, LLMBackend)
        assert backend.backend_type == "mock"
    finally:
        reset_backend_cache()


def test_mock_backend_health_check_reports_warning():
    backend = MockBackend()
    health = backend.health_check_sync()
    assert health["status"] == "online"
    assert health["backend"] == "mock"
    assert "warning" in health["details"]
    assert "canned" in health["details"]["warning"].lower()


def test_mock_backend_info_includes_warning():
    backend = MockBackend()
    info = backend.info()
    assert info["backend"] == "mock"
    assert "warning" in info
    assert "canned" in info["warning"].lower()


# ---------------------------------------------------------------------------
# Capacity declarations — the planner reads these directly off the backend
# ---------------------------------------------------------------------------
def test_mock_backend_advertises_high_capacity():
    backend = MockBackend()
    assert backend.max_planning_chunk_size == 50
    assert backend.max_context_tokens == 32_000
    assert backend.supports_concurrent_calls is True
    assert backend.max_concurrent_calls == 5


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_chat_stream_yields_full_content_as_one_chunk():
    backend = MockBackend()
    messages = [{"role": "user", "content": "categorize"}]
    chunks = []
    async for c in backend.chat_stream(messages):
        chunks.append(c)
    assert len(chunks) == 1
    # Stream chunk should be JSON-decodable.
    json.loads(chunks[0])
