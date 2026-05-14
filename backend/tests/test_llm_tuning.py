"""Tests for the LLM tuning fixes — context window, timeouts, retry
backoff, and categorizer batch sizing.

These cover the changes made to address the production
"truncating input prompt limit=4096 prompt=10420" Ollama warning and
the cascading 120s read-timeout failures it caused.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from app.core.config import Settings
from app.core.llm.base import LLMBackendError
from app.core.llm.factory import get_llm_backend, reset_backend_cache
from app.core.llm.ollama_backend import OllamaBackend


# ---------------------------------------------------------------------------
# Mocking helpers — copied from test_llm_backends to keep the suite
# independent.
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


def _mock_async_client(post_response=None, post_raises=None, post_side_effect=None):
    """Returns a context-manager mock that yields a client whose
    ``post`` returns ``post_response`` or raises ``post_raises``."""
    from unittest.mock import AsyncMock, MagicMock

    client = MagicMock()
    if post_side_effect is not None:
        client.post = AsyncMock(side_effect=post_side_effect)
    elif post_raises is not None:
        client.post = AsyncMock(side_effect=post_raises)
    else:
        client.post = AsyncMock(return_value=post_response)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm, client


# ---------------------------------------------------------------------------
# OllamaBackend — num_ctx + timeout config
# ---------------------------------------------------------------------------
def test_ollama_backend_passes_num_ctx_in_options():
    backend = OllamaBackend(num_ctx=8192, max_retries=0)
    cm, client = _mock_async_client(
        post_response=_Resp(200, {"message": {"content": "ok"}, "model": "llama3:8b"})
    )
    with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
        asyncio.run(backend.chat(messages=[{"role": "user", "content": "hi"}]))
    sent = client.post.call_args.kwargs["json"]
    assert sent["options"]["num_ctx"] == 8192


def test_ollama_backend_num_ctx_overridable():
    backend = OllamaBackend(num_ctx=16384, max_retries=0)
    cm, client = _mock_async_client(post_response=_Resp(200, {"message": {"content": "ok"}}))
    with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
        asyncio.run(backend.chat(messages=[{"role": "user", "content": "hi"}]))
    assert client.post.call_args.kwargs["json"]["options"]["num_ctx"] == 16384


def test_ollama_backend_constructs_httpx_timeout_with_long_read():
    backend = OllamaBackend(timeout=600.0, connect_timeout=30.0)
    t = backend._httpx_timeout()
    # httpx.Timeout exposes per-leg attributes after construction.
    assert t.read == 600.0
    assert t.connect == 30.0


def test_ollama_backend_warns_when_prompt_approaches_context(caplog):
    backend = OllamaBackend(num_ctx=1000, max_retries=0)
    # Threshold is 80% of num_ctx (= 800 tokens). 4 chars/token, so a
    # 4000-char message is exactly 1000 tokens — well over threshold.
    big_message = [{"role": "user", "content": "x" * 4000}]
    cm, _ = _mock_async_client(post_response=_Resp(200, {"message": {"content": "ok"}}))
    with caplog.at_level("WARNING"):
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
            asyncio.run(backend.chat(messages=big_message))
    assert any("approaches context window" in r.message for r in caplog.records)


def test_ollama_backend_does_not_warn_for_small_prompts(caplog):
    backend = OllamaBackend(num_ctx=8192, max_retries=0)
    cm, _ = _mock_async_client(post_response=_Resp(200, {"message": {"content": "ok"}}))
    with caplog.at_level("WARNING"):
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
            asyncio.run(backend.chat(messages=[{"role": "user", "content": "hi"}]))
    assert not any("approaches context window" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Retry backoff
# ---------------------------------------------------------------------------
def test_retry_recovers_after_transient_timeout():
    backend = OllamaBackend(max_retries=2)
    # First call raises TimeoutException, second succeeds.
    cm = None
    success = _Resp(200, {"message": {"content": "recovered"}, "model": "x"})
    side_effects = [httpx.ReadTimeout("slow"), success]

    cm, client = _mock_async_client(post_side_effect=side_effects)
    with patch("app.core.llm.ollama_backend.asyncio.sleep") as mock_sleep:

        async def _fake_sleep(s):
            return None

        mock_sleep.side_effect = _fake_sleep
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
            result = asyncio.run(backend.chat(messages=[]))
    assert result["content"] == "recovered"
    # First retry waits 5s.
    assert mock_sleep.call_args_list[0].args[0] == 5.0


def test_retry_uses_5s_then_30s_backoff_schedule():
    backend = OllamaBackend(max_retries=2)
    cm, client = _mock_async_client(
        post_side_effect=[
            httpx.ReadTimeout("1"),
            httpx.ReadTimeout("2"),
            _Resp(200, {"message": {"content": "ok"}}),
        ]
    )
    with patch("app.core.llm.ollama_backend.asyncio.sleep") as mock_sleep:

        async def _fake_sleep(s):
            return None

        mock_sleep.side_effect = _fake_sleep
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
            asyncio.run(backend.chat(messages=[]))
    waits = [call.args[0] for call in mock_sleep.call_args_list]
    assert waits == [5.0, 30.0]


def test_retry_gives_up_after_max_retries():
    backend = OllamaBackend(max_retries=2)
    # Every attempt times out — total 3 attempts (initial + 2 retries).
    cm, client = _mock_async_client(post_raises=httpx.ReadTimeout("always slow"))
    with patch("app.core.llm.ollama_backend.asyncio.sleep") as mock_sleep:

        async def _fake_sleep(s):
            return None

        mock_sleep.side_effect = _fake_sleep
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
            with pytest.raises(LLMBackendError, match="after 3 attempts"):
                asyncio.run(backend.chat(messages=[]))
    # 3 attempts → 2 sleeps.
    assert mock_sleep.call_count == 2


def test_retry_does_not_kick_in_for_4xx_errors():
    """A 4xx response is deterministic — retrying won't change the
    answer, so the backoff path should be skipped."""
    backend = OllamaBackend(max_retries=2)
    err_resp = _Resp(400, {"error": "bad request"})
    cm, client = _mock_async_client(post_response=err_resp)
    with patch("app.core.llm.ollama_backend.asyncio.sleep") as mock_sleep:
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
            with pytest.raises(LLMBackendError, match="HTTP 400"):
                asyncio.run(backend.chat(messages=[]))
    assert mock_sleep.call_count == 0


def test_retry_disabled_when_max_retries_zero():
    backend = OllamaBackend(max_retries=0)
    cm, client = _mock_async_client(post_raises=httpx.ReadTimeout("once"))
    with patch("app.core.llm.ollama_backend.asyncio.sleep") as mock_sleep:
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=cm):
            with pytest.raises(LLMBackendError):
                asyncio.run(backend.chat(messages=[]))
    assert mock_sleep.call_count == 0


# ---------------------------------------------------------------------------
# Factory plumbs new settings through
# ---------------------------------------------------------------------------
def test_factory_passes_num_ctx_and_timeout_from_settings():
    reset_backend_cache()
    cfg = Settings(
        ollama_host="http://test:11434",
        ollama_model="test-model",
        ollama_num_ctx=4096,
        llm_read_timeout=300.0,
        llm_connect_timeout=10.0,
        llm_max_retries=1,
    )
    backend = get_llm_backend(cfg)
    assert isinstance(backend, OllamaBackend)
    assert backend.num_ctx == 4096
    assert backend.read_timeout == 300.0
    assert backend.connect_timeout == 10.0
    assert backend.max_retries == 1
    reset_backend_cache()


# ---------------------------------------------------------------------------
# Categorizer batching at scale
# ---------------------------------------------------------------------------
def _make_vms(db_session, vcenter_id: int, n: int) -> list:
    from app.models.vm import VM

    vms = []
    for i in range(n):
        vm = VM(
            name=f"vm-{i:03d}",
            source_hostname=f"vm-{i:03d}.local",
            ip_address=f"10.0.{i // 256}.{i % 256}",
            source_vcenter_id=vcenter_id,
        )
        db_session.add(vm)
        vms.append(vm)
    db_session.commit()
    for vm in vms:
        db_session.refresh(vm)
    return vms


def test_categorizer_default_batch_size_is_ten():
    """Regression guard: 100-VM default was the v0.1.x bug that
    overflowed Ollama's 4096 default context window."""
    from app.core import categorizer

    assert categorizer.DEFAULT_BATCH_SIZE == 10


def test_categorizer_splits_large_inventory_into_multiple_batches(db_session):
    """57 VMs at batch_size=10 → 6 batches (10, 10, 10, 10, 10, 7)."""
    from app.core.categorizer import categorize
    from app.models.vcenter import VCenterSource

    vc = VCenterSource(name="vc-batch", hostname="vc-batch.local")
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    _make_vms(db_session, vc.id, 57)

    captured_batches: list[int] = []

    class _RecordingBackend:
        def chat_sync(self, *, messages, **kwargs):
            # Extract the batch size the categorizer sent and record it.
            user = next(m for m in messages if m["role"] == "user")
            payload = json.loads(user["content"].split("\n\n", 1)[1])
            captured_batches.append(payload["batch_size"])
            return {
                "content": json.dumps(
                    {
                        "groups": [
                            {
                                "kind": "application",
                                "name": "app-x",
                                "description": "test",
                                "members": [
                                    {"vm_id": v["vm_id"], "confidence": 1.0, "rationale": "n"}
                                    for v in payload["vms"]
                                ],
                            },
                            {
                                "kind": "environment",
                                "name": "prod",
                                "description": "test",
                                "members": [
                                    {"vm_id": v["vm_id"], "confidence": 1.0, "rationale": "n"}
                                    for v in payload["vms"]
                                ],
                            },
                            {
                                "kind": "business_unit",
                                "name": "default",
                                "description": "test",
                                "members": [
                                    {"vm_id": v["vm_id"], "confidence": 1.0, "rationale": "n"}
                                    for v in payload["vms"]
                                ],
                            },
                        ]
                    }
                ),
                "model": "test-model",
            }

    result = categorize(
        db_session,
        source_vcenter_id=vc.id,
        backend=_RecordingBackend(),
        batch_size=10,
    )
    assert result["batches_processed"] == 6
    assert captured_batches == [10, 10, 10, 10, 10, 7]


def test_categorizer_token_budget_per_batch_stays_under_num_ctx(db_session):
    """Each batch's prompt should fit comfortably inside num_ctx=8192.

    Approximation: chars / 4 ≈ tokens. The batched prompt is
    system_prompt + JSON-serialized 10-VM payload. We assert the
    upper bound stays under 80% of 8192 (the guardrail threshold)
    so the warning path isn't triggered in normal operation.
    """
    from app.core.categorizer import SYSTEM_PROMPT, _vm_lite_for_prompt
    from app.models.vcenter import VCenterSource

    vc = VCenterSource(name="vc-budget", hostname="vc-budget.local")
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)
    vms = _make_vms(db_session, vc.id, 10)

    payload = {
        "vcenter": vc.id,
        "batch_size": len(vms),
        "vms": [_vm_lite_for_prompt(vm) for vm in vms],
    }
    user_content = (
        "Categorize the following VMs and return the JSON object "
        "specified in the system prompt:\n\n" + json.dumps(payload, indent=2, sort_keys=True)
    )
    approx_tokens = (len(SYSTEM_PROMPT) + len(user_content)) // 4
    # 80% of the 8192 default num_ctx — staying under this avoids the
    # guardrail warning + leaves ~1600 tokens for the JSON response.
    assert approx_tokens < int(8192 * 0.8), (
        f"10-VM batch should fit under context budget; got " f"approx_tokens={approx_tokens}"
    )
