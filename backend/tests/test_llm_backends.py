"""Unit tests for the LLM backend implementations.

Mocks the underlying httpx client so the tests run hermetically — no
network. Each backend gets the same canonical battery: chat round-trip,
health probe, error handling.

Factory tests verify that the right backend gets instantiated for each
``LLM_BACKEND_TYPE`` value.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import Settings
from app.core.llm.base import (
    LLMAuthError,
    LLMBackendError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnreachableError,
)
from app.core.llm.factory import (
    get_llm_backend,
    reset_backend_cache,
)
from app.core.llm.kserve_backend import KServeBackend
from app.core.llm.ollama_backend import OllamaBackend
from app.core.llm.vllm_backend import VLLMBackend


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """Drop the factory cache between tests so settings overrides take."""
    reset_backend_cache()
    yield
    reset_backend_cache()


def _mock_async_client(*, post_response=None, get_response=None, post_raises=None, get_raises=None):
    """Build a MagicMock that mimics httpx.AsyncClient as a context manager.

    The httpx surface uses ``async with`` to enter a client, then awaits
    ``post`` / ``get`` on it. AsyncMock handles the coroutine returns;
    MagicMock owns the context-manager dance.
    """
    instance = MagicMock()
    if post_raises is not None:
        instance.post = AsyncMock(side_effect=post_raises)
    else:
        instance.post = AsyncMock(return_value=post_response)
    if get_raises is not None:
        instance.get = AsyncMock(side_effect=get_raises)
    else:
        instance.get = AsyncMock(return_value=get_response)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    return instance


def _ok(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=body)
    return resp


# ---------------------------------------------------------------------------
# OllamaBackend
# ---------------------------------------------------------------------------
class TestOllamaBackend:
    def test_chat_posts_to_api_chat_and_returns_content(self):
        backend = OllamaBackend(base_url="http://ollama:11434", default_model="llama3:8b")
        mock_client = _mock_async_client(
            post_response=_ok({"message": {"content": "hello"}, "model": "llama3:8b"})
        )
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(backend.chat(messages=[{"role": "user", "content": "hi"}]))

        assert result["content"] == "hello"
        assert result["model"] == "llama3:8b"

        # URL + payload shape are what Ollama expects.
        call = mock_client.post.call_args
        assert call.args[0] == "http://ollama:11434/api/chat"
        sent = call.kwargs["json"]
        assert sent["model"] == "llama3:8b"
        assert sent["stream"] is False
        assert sent["format"] == "json"
        assert sent["options"]["temperature"] == 0.1
        assert sent["messages"] == [{"role": "user", "content": "hi"}]

    def test_chat_raises_unreachable_on_connect_error(self):
        # max_retries=0 keeps the test fast — retry-with-backoff behavior
        # is exercised in test_retry.py.
        backend = OllamaBackend(max_retries=0)
        mock_client = _mock_async_client(post_raises=httpx.ConnectError("refused"))
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMUnreachableError, match="Cannot reach Ollama"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_timeout_on_read_timeout(self):
        backend = OllamaBackend(max_retries=0)
        mock_client = _mock_async_client(post_raises=httpx.ReadTimeout("slow"))
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMTimeoutError, match="timed out"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_auth_on_401(self):
        backend = OllamaBackend(max_retries=0)
        resp = MagicMock()
        resp.status_code = 401
        err = httpx.HTTPStatusError("unauthorized", request=MagicMock(), response=resp)
        # raise_for_status is what triggers HTTPStatusError; simulate by
        # making the post() return a response whose raise_for_status raises.
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        bad_resp.response = resp
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMAuthError, match="refused auth"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_response_error_on_5xx(self):
        backend = OllamaBackend(max_retries=0)
        resp = MagicMock()
        resp.status_code = 503
        err = httpx.HTTPStatusError("svc unavail", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMResponseError, match="HTTP 503"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_response_error_on_empty_content(self):
        backend = OllamaBackend()
        mock_client = _mock_async_client(post_response=_ok({"message": {"content": ""}}))
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMResponseError, match="empty message"):
                asyncio.run(backend.chat(messages=[]))

    def test_typed_exceptions_subclass_llm_backend_error(self):
        # Callers that still catch the base class must keep working.
        assert issubclass(LLMUnreachableError, LLMBackendError)
        assert issubclass(LLMAuthError, LLMBackendError)
        assert issubclass(LLMTimeoutError, LLMBackendError)
        assert issubclass(LLMResponseError, LLMBackendError)

    def test_health_check_reports_online_with_latency(self):
        backend = OllamaBackend(default_model="llama3:8b")
        mock_client = _mock_async_client(
            get_response=_ok({"models": [{"name": "llama3:8b"}, {"name": "mistral:latest"}]})
        )
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            health = asyncio.run(backend.health_check())

        assert health["status"] == "online"
        assert health["backend"] == "ollama"
        assert health["model"] == "llama3:8b"
        assert health["details"]["available_models"] == ["llama3:8b", "mistral:latest"]
        assert health["details"]["model_loaded"] is True
        assert health["latency_ms"] >= 0

    def test_health_check_reports_offline_on_connect_error(self):
        backend = OllamaBackend()
        mock_client = _mock_async_client(get_raises=httpx.ConnectError("refused"))
        with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
            health = asyncio.run(backend.health_check())

        assert health["status"] == "offline"
        assert health["latency_ms"] == -1
        assert "refused" in health["details"]["error"]


# ---------------------------------------------------------------------------
# KServeBackend
# ---------------------------------------------------------------------------
class TestKServeBackend:
    def test_chat_posts_to_openai_compatible_endpoint(self):
        backend = KServeBackend(
            endpoint="https://granite.ns.svc.cluster.local",
            model_name="granite-3-8b-instruct",
            token="abc123",
            verify_ssl=True,
        )
        mock_client = _mock_async_client(
            post_response=_ok(
                {
                    "choices": [{"message": {"role": "assistant", "content": "hi from granite"}}],
                    "model": "granite-3-8b-instruct",
                }
            )
        )
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(backend.chat(messages=[{"role": "user", "content": "hi"}]))

        assert result["content"] == "hi from granite"
        assert result["model"] == "granite-3-8b-instruct"

        call = mock_client.post.call_args
        assert call.args[0] == "https://granite.ns.svc.cluster.local/v1/chat/completions"
        # Auth header is wired up.
        assert call.kwargs["headers"]["Authorization"] == "Bearer abc123"
        sent = call.kwargs["json"]
        assert sent["model"] == "granite-3-8b-instruct"
        assert sent["stream"] is False

    def test_chat_with_max_tokens_passes_through(self):
        backend = KServeBackend(endpoint="https://k", model_name="m")
        mock_client = _mock_async_client(
            post_response=_ok({"choices": [{"message": {"content": "ok"}}], "model": "m"})
        )
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(backend.chat(messages=[], max_tokens=512))

        sent = mock_client.post.call_args.kwargs["json"]
        assert sent["max_tokens"] == 512

    def test_chat_raises_response_error_when_choices_empty(self):
        backend = KServeBackend(endpoint="https://k", model_name="m")
        mock_client = _mock_async_client(post_response=_ok({"choices": []}))
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMResponseError, match="no choices"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_timeout_on_connect_timeout(self):
        backend = KServeBackend(endpoint="https://k", model_name="m")
        mock_client = _mock_async_client(post_raises=httpx.ConnectTimeout("timeout"))
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMTimeoutError, match="timed out"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_unreachable_on_transport_error(self):
        backend = KServeBackend(endpoint="https://k", model_name="m")
        mock_client = _mock_async_client(post_raises=httpx.ConnectError("dns fail"))
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMUnreachableError, match="Cannot reach KServe"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_auth_on_403(self):
        backend = KServeBackend(endpoint="https://k", model_name="m")
        resp = MagicMock()
        resp.status_code = 403
        err = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMAuthError, match="refused auth"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_response_error_on_5xx(self):
        backend = KServeBackend(endpoint="https://k", model_name="m")
        resp = MagicMock()
        resp.status_code = 502
        err = httpx.HTTPStatusError("bad gw", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMResponseError, match="HTTP 502"):
                asyncio.run(backend.chat(messages=[]))

    def test_constructor_rejects_missing_endpoint(self):
        with pytest.raises(LLMBackendError, match="KSERVE_ENDPOINT"):
            KServeBackend(endpoint="", model_name="m")

    def test_constructor_rejects_missing_model_name(self):
        with pytest.raises(LLMBackendError, match="KSERVE_MODEL_NAME"):
            KServeBackend(endpoint="https://k", model_name="")

    def test_token_resolution_prefers_explicit_token_over_file(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("file-token")
        backend = KServeBackend(
            endpoint="https://k",
            model_name="m",
            token="explicit-token",
            token_file=str(token_file),
        )
        assert backend._resolve_token() == "explicit-token"

    def test_token_resolution_falls_back_to_token_file(self, tmp_path):
        token_file = tmp_path / "token"
        token_file.write_text("file-token\n")  # trailing newline gets stripped
        backend = KServeBackend(
            endpoint="https://k", model_name="m", token=None, token_file=str(token_file)
        )
        assert backend._resolve_token() == "file-token"

    def test_token_resolution_returns_none_when_unavailable(self, tmp_path):
        backend = KServeBackend(
            endpoint="https://k",
            model_name="m",
            token=None,
            token_file=str(tmp_path / "missing"),
        )
        assert backend._resolve_token() is None

    def test_health_check_reports_online_and_lists_models(self):
        backend = KServeBackend(endpoint="https://k", model_name="granite")
        mock_client = _mock_async_client(
            get_response=_ok({"data": [{"id": "granite"}, {"id": "alt-model"}]})
        )
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            health = asyncio.run(backend.health_check())

        assert health["status"] == "online"
        assert health["backend"] == "kserve"
        assert health["details"]["available_models"] == ["granite", "alt-model"]
        assert health["details"]["model_loaded"] is True

    def test_health_check_reports_offline_on_failure(self):
        backend = KServeBackend(endpoint="https://k", model_name="m")
        mock_client = _mock_async_client(
            get_raises=httpx.HTTPStatusError("500", request=MagicMock(), response=MagicMock())
        )
        with patch("app.core.llm.kserve_backend.httpx.AsyncClient", return_value=mock_client):
            health = asyncio.run(backend.health_check())

        assert health["status"] == "offline"


# ---------------------------------------------------------------------------
# VLLMBackend
# ---------------------------------------------------------------------------
class TestVLLMBackend:
    def test_chat_raises_not_implemented(self):
        backend = VLLMBackend(endpoint="https://vllm", model_name="m")
        with pytest.raises(NotImplementedError, match="v1.0.0"):
            asyncio.run(backend.chat(messages=[]))

    def test_health_check_reports_offline_with_planned_release(self):
        backend = VLLMBackend(endpoint="https://vllm", model_name="m")
        health = asyncio.run(backend.health_check())
        assert health["status"] == "offline"
        assert health["details"]["planned_release"] == "v1.0.0"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
class TestFactory:
    def test_default_returns_ollama_backend(self):
        cfg = Settings(llm_backend_type="ollama")
        backend = get_llm_backend(cfg)
        assert isinstance(backend, OllamaBackend)
        assert backend.endpoint == cfg.ollama_host.rstrip("/")
        assert backend.default_model == cfg.ollama_model

    def test_kserve_type_returns_kserve_backend(self):
        cfg = Settings(
            llm_backend_type="kserve",
            kserve_endpoint="https://granite.ns.svc.cluster.local",
            kserve_model_name="granite-3-8b-instruct",
            kserve_token="tok",
        )
        backend = get_llm_backend(cfg)
        assert isinstance(backend, KServeBackend)
        assert backend.endpoint == "https://granite.ns.svc.cluster.local"
        assert backend.default_model == "granite-3-8b-instruct"

    def test_vllm_type_returns_vllm_backend(self):
        cfg = Settings(
            llm_backend_type="vllm",
            vllm_endpoint="https://vllm",
            vllm_model_name="some-model",
        )
        backend = get_llm_backend(cfg)
        assert isinstance(backend, VLLMBackend)

    def test_unknown_type_raises_with_supported_list(self):
        cfg = Settings(llm_backend_type="cohere")
        with pytest.raises(LLMBackendError, match="Unknown LLM_BACKEND_TYPE"):
            get_llm_backend(cfg)

    def test_kserve_without_endpoint_fails_loud(self):
        cfg = Settings(llm_backend_type="kserve", kserve_endpoint=None)
        with pytest.raises(LLMBackendError, match="KSERVE_ENDPOINT"):
            get_llm_backend(cfg)

    def test_case_insensitive_dispatch(self):
        cfg = Settings(llm_backend_type="OLLAMA")
        backend = get_llm_backend(cfg)
        assert isinstance(backend, OllamaBackend)


# ---------------------------------------------------------------------------
# Sync bridge
# ---------------------------------------------------------------------------
def test_chat_sync_round_trips_through_async_chat():
    """The sync wrapper must call the async chat exactly once."""
    backend = OllamaBackend(base_url="http://ollama:11434", default_model="llama3:8b")
    mock_client = _mock_async_client(
        post_response=_ok({"message": {"content": "sync-ok"}, "model": "llama3:8b"})
    )
    with patch("app.core.llm.ollama_backend.httpx.AsyncClient", return_value=mock_client):
        result = backend.chat_sync(messages=[{"role": "user", "content": "x"}])

    assert result["content"] == "sync-ok"
    assert mock_client.post.call_count == 1


def test_info_returns_static_config():
    backend = KServeBackend(endpoint="https://granite", model_name="granite-3-8b-instruct")
    info = backend.info()
    assert info == {
        "backend": "kserve",
        "model": "granite-3-8b-instruct",
        "endpoint": "https://granite",
    }


# ---------------------------------------------------------------------------
# Startup health-check log lines
# ---------------------------------------------------------------------------
class TestStartupHealthCheckLog:
    """The lifespan probe must log exactly ``llm.startup.ok`` on success
    or ``llm.startup.FAILED`` on failure. Operators grep the pod log for
    these prefixes — keep them stable."""

    def test_logs_ok_when_backend_online(self, caplog):
        import logging

        from app.main import _report_llm_status

        fake_backend = MagicMock()
        fake_backend.health_check = AsyncMock(
            return_value={
                "status": "online",
                "backend": "kserve",
                "endpoint": "https://granite",
                "model": "granite-3-8b-instruct",
                "latency_ms": 42,
                "details": {"available_models": ["granite-3-8b-instruct"]},
            }
        )
        with (
            patch("app.main.get_llm_backend", return_value=fake_backend),
            caplog.at_level(logging.INFO, logger="app.main"),
        ):
            asyncio.run(_report_llm_status())

        msgs = [r.message for r in caplog.records]
        assert any("llm.startup.ok" in m for m in msgs)
        assert any("https://granite" in m for m in msgs)

    def test_logs_failed_when_backend_offline(self, caplog):
        import logging

        from app.main import _report_llm_status

        fake_backend = MagicMock()
        fake_backend.health_check = AsyncMock(
            return_value={
                "status": "offline",
                "backend": "kserve",
                "endpoint": "https://wrong",
                "model": "granite",
                "latency_ms": -1,
                "details": {"error": "connection refused"},
            }
        )
        with (
            patch("app.main.get_llm_backend", return_value=fake_backend),
            caplog.at_level(logging.ERROR, logger="app.main"),
        ):
            asyncio.run(_report_llm_status())

        msgs = [r.message for r in caplog.records]
        assert any("llm.startup.FAILED" in m for m in msgs)
        assert any("connection refused" in m for m in msgs)

    def test_logs_failed_when_health_check_raises(self, caplog):
        import logging

        from app.main import _report_llm_status

        fake_backend = MagicMock()
        fake_backend.health_check = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch("app.main.get_llm_backend", return_value=fake_backend),
            caplog.at_level(logging.ERROR, logger="app.main"),
        ):
            asyncio.run(_report_llm_status())

        # Even if health_check throws, the API must still come up — the
        # function returns rather than re-raising.
        msgs = [r.message for r in caplog.records]
        assert any("llm.startup.FAILED" in m for m in msgs)
        assert any("boom" in m for m in msgs)
