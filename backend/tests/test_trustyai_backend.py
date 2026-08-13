"""Tests for the TrustyAI Guardrails Orchestrator backend.

Mocks httpx so the tests run hermetically. Covers:
  - the happy path (OpenAI-style choices → content),
  - the guardrail-flagged path (200 + warnings → LLMGuardrailError + detections),
  - the detectors block in the outgoing request,
  - secret redaction (key never in __repr__ or exception messages),
  - auth being optional.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.llm.base import LLMAuthError, LLMGuardrailError, LLMResponseError
from app.core.llm.trustyai_backend import TrustyAIBackend

_PATCH = "app.core.llm.trustyai_backend.httpx.AsyncClient"


def _mock_async_client(*, post_response=None, post_raises=None):
    instance = MagicMock()
    if post_raises is not None:
        instance.post = AsyncMock(side_effect=post_raises)
    else:
        instance.post = AsyncMock(return_value=post_response)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


def _ok(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=body)
    return resp


def _backend(**kw) -> TrustyAIBackend:
    return TrustyAIBackend(
        base_url="https://orch.example/",
        model_name="granite",
        api_key="super-secret-key",
        **kw,
    )


def test_chat_happy_path_returns_content_and_posts_detectors():
    backend = _backend(input_detectors="prompt_injection", output_detectors="hap")
    mock = _mock_async_client(
        post_response=_ok(
            {"model": "granite", "choices": [{"message": {"content": "clean answer"}}]}
        )
    )
    with patch(_PATCH, return_value=mock):
        result = asyncio.run(backend.chat(messages=[{"role": "user", "content": "hi"}]))

    assert result["content"] == "clean answer"
    call = mock.post.call_args
    # Correct endpoint path.
    assert call.args[0] == "https://orch.example/api/v2/chat/completions-detection"
    sent = call.kwargs["json"]
    assert sent["detectors"] == {"input": {"prompt_injection": {}}, "output": {"hap": {}}}
    # Auth header present (key configured) but the key is in the header only.
    assert call.kwargs["headers"]["Authorization"] == "Bearer super-secret-key"


def test_chat_raises_guardrail_error_with_detections_on_warning():
    backend = _backend()
    body = {
        "model": "granite",
        "detections": {
            "input": [
                {
                    "message_index": 0,
                    "results": [{"detector_id": "prompt_injection", "score": 0.97}],
                }
            ]
        },
        "warnings": [{"type": "UNSUITABLE_INPUT", "message": "Unsuitable input detected."}],
    }
    mock = _mock_async_client(post_response=_ok(body))
    with patch(_PATCH, return_value=mock):
        with pytest.raises(LLMGuardrailError) as ei:
            asyncio.run(backend.chat(messages=[{"role": "user", "content": "evil"}]))
    err = ei.value
    assert "UNSUITABLE_INPUT" in str(err)
    assert err.detections["input"][0]["results"][0]["detector_id"] == "prompt_injection"


def test_chat_raises_auth_on_401():
    backend = _backend()
    resp = MagicMock()
    resp.status_code = 401
    err = httpx.HTTPStatusError("unauthorized", request=MagicMock(), response=resp)
    bad = MagicMock()
    bad.raise_for_status = MagicMock(side_effect=err)
    mock = _mock_async_client(post_response=bad)
    with patch(_PATCH, return_value=mock):
        with pytest.raises(LLMAuthError):
            asyncio.run(backend.chat(messages=[]))


def test_chat_raises_response_error_on_empty_choices():
    backend = _backend()
    mock = _mock_async_client(post_response=_ok({"model": "granite", "choices": []}))
    with patch(_PATCH, return_value=mock):
        with pytest.raises(LLMResponseError):
            asyncio.run(backend.chat(messages=[]))


def test_api_key_is_optional():
    # In-cluster orchestrator may be unauthenticated — no key → no auth header.
    backend = TrustyAIBackend(base_url="https://orch.example", model_name="granite", api_key=None)
    mock = _mock_async_client(
        post_response=_ok({"model": "granite", "choices": [{"message": {"content": "ok"}}]})
    )
    with patch(_PATCH, return_value=mock):
        asyncio.run(backend.chat(messages=[]))
    assert "Authorization" not in mock.post.call_args.kwargs["headers"]


def test_repr_redacts_api_key():
    backend = _backend()
    r = repr(backend)
    assert "super-secret-key" not in r
    assert "bearer:****" in r


def test_auth_error_message_does_not_leak_key():
    backend = _backend()
    resp = MagicMock()
    resp.status_code = 403
    err = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=resp)
    bad = MagicMock()
    bad.raise_for_status = MagicMock(side_effect=err)
    mock = _mock_async_client(post_response=bad)
    with patch(_PATCH, return_value=mock):
        with pytest.raises(LLMAuthError) as ei:
            asyncio.run(backend.chat(messages=[]))
    assert "super-secret-key" not in str(ei.value)
