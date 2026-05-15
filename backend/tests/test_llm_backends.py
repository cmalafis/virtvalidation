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
from app.core.llm.maas_backend import MaaSBackend
from app.core.llm.ollama_backend import OllamaBackend
from app.core.llm.runtime import invalidate as invalidate_runtime_cache
from app.core.llm.types import LLMBackendType
from app.core.llm.vllm_backend import VLLMBackend


@pytest.fixture(autouse=True)
def _reset_factory_cache():
    """Drop the factory cache between tests so settings overrides take."""
    reset_backend_cache()
    invalidate_runtime_cache()
    yield
    reset_backend_cache()
    invalidate_runtime_cache()


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
# MaaSBackend
# ---------------------------------------------------------------------------
class TestMaaSBackend:
    def _backend(self, **overrides):
        defaults = {
            "base_url": "https://litellm.example.com/v1",
            "model_name": "granite-32-8b-instruct",
            "api_key": "sk-test-XXXXXXXX",
        }
        defaults.update(overrides)
        return MaaSBackend(**defaults)

    def test_chat_posts_to_openai_compatible_endpoint(self):
        backend = self._backend()
        mock_client = _mock_async_client(
            post_response=_ok(
                {
                    "choices": [{"message": {"role": "assistant", "content": "hi from granite"}}],
                    "model": "granite-32-8b-instruct",
                }
            )
        )
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            result = asyncio.run(backend.chat(messages=[{"role": "user", "content": "hi"}]))

        assert result["content"] == "hi from granite"
        assert result["model"] == "granite-32-8b-instruct"

        call = mock_client.post.call_args
        # Base URL already includes /v1 — backend appends /chat/completions only.
        assert call.args[0] == "https://litellm.example.com/v1/chat/completions"
        # Bearer auth is sent on every call.
        assert call.kwargs["headers"]["Authorization"] == "Bearer sk-test-XXXXXXXX"
        sent = call.kwargs["json"]
        assert sent["model"] == "granite-32-8b-instruct"
        assert sent["stream"] is False
        assert sent["temperature"] == 0.1

    def test_chat_with_max_tokens_passes_through(self):
        backend = self._backend()
        mock_client = _mock_async_client(
            post_response=_ok({"choices": [{"message": {"content": "ok"}}], "model": "granite"})
        )
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            asyncio.run(backend.chat(messages=[], max_tokens=512))

        sent = mock_client.post.call_args.kwargs["json"]
        assert sent["max_tokens"] == 512

    def test_chat_raises_auth_on_401(self):
        backend = self._backend()
        resp = MagicMock()
        resp.status_code = 401
        err = httpx.HTTPStatusError("unauth", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMAuthError, match="refused auth"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_auth_on_403(self):
        backend = self._backend()
        resp = MagicMock()
        resp.status_code = 403
        err = httpx.HTTPStatusError("forbidden", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMAuthError):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_response_error_on_5xx(self):
        backend = self._backend()
        resp = MagicMock()
        resp.status_code = 502
        err = httpx.HTTPStatusError("bad gw", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMResponseError, match="HTTP 502"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_distinguishes_429_rate_limit(self):
        backend = self._backend()
        resp = MagicMock()
        resp.status_code = 429
        err = httpx.HTTPStatusError("rate limit", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMResponseError, match="rate-limited"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_unreachable_on_transport_error(self):
        backend = self._backend()
        mock_client = _mock_async_client(post_raises=httpx.ConnectError("dns fail"))
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMUnreachableError, match="Cannot reach MaaS"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_timeout_on_read_timeout(self):
        backend = self._backend()
        mock_client = _mock_async_client(post_raises=httpx.ReadTimeout("slow"))
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMTimeoutError, match="timed out"):
                asyncio.run(backend.chat(messages=[]))

    def test_chat_raises_response_error_on_empty_choices(self):
        backend = self._backend()
        mock_client = _mock_async_client(post_response=_ok({"choices": []}))
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMResponseError, match="no choices"):
                asyncio.run(backend.chat(messages=[]))

    def test_constructor_rejects_missing_base_url(self):
        with pytest.raises(LLMBackendError, match="LLM_MAAS_BASE_URL"):
            MaaSBackend(base_url="", model_name="m", api_key="k")

    def test_constructor_rejects_missing_model(self):
        with pytest.raises(LLMBackendError, match="LLM_MAAS_MODEL"):
            MaaSBackend(base_url="https://x/v1", model_name="", api_key="k")

    def test_constructor_rejects_missing_api_key(self):
        with pytest.raises(LLMBackendError, match="LLM_MAAS_API_KEY"):
            MaaSBackend(base_url="https://x/v1", model_name="m", api_key="")

    def test_repr_redacts_api_key(self):
        backend = self._backend(api_key="sk-leak-test-DEADBEEF")
        rendered = repr(backend)
        assert "sk-leak-test-DEADBEEF" not in rendered
        assert "DEADBEEF" not in rendered
        assert "bearer:****" in rendered

    def test_does_not_leak_api_key_on_auth_failure(self, caplog):
        """The MaaS API key MUST NOT appear in any exception message,
        repr, or log output. Federal customer security review will
        reject any code path that lets a bearer token escape — even
        when the request fails."""
        import logging

        secret_key = "sk-leak-test-DEADBEEFCAFE12345678"
        backend = MaaSBackend(
            base_url="https://litellm.example.com/v1",
            model_name="granite-32-8b-instruct",
            api_key=secret_key,
        )
        resp = MagicMock()
        resp.status_code = 401
        err = httpx.HTTPStatusError("unauth", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(post_response=bad_resp)

        caplog.set_level(logging.DEBUG)
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(LLMAuthError) as excinfo:
                asyncio.run(backend.chat(messages=[]))

        # Exception text + cause chain must not contain the key.
        assert secret_key not in str(excinfo.value)
        assert secret_key not in repr(excinfo.value)
        cause = excinfo.value.__cause__
        if cause is not None:  # __cause__ is suppressed for MaaS auth path
            assert secret_key not in str(cause)
            assert secret_key not in repr(cause)
        # No log line emitted by the backend should contain the key.
        assert secret_key not in caplog.text
        # And repr() of the backend itself must redact.
        assert secret_key not in repr(backend)

    def test_health_check_reports_online_and_lists_models(self):
        backend = self._backend()
        mock_client = _mock_async_client(
            get_response=_ok({"data": [{"id": "granite-32-8b-instruct"}, {"id": "llama-3-70b"}]})
        )
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            health = asyncio.run(backend.health_check())

        assert health["status"] == "online"
        assert health["backend"] == "maas"
        assert health["details"]["available_models"] == [
            "granite-32-8b-instruct",
            "llama-3-70b",
        ]
        assert health["details"]["model_loaded"] is True
        # The /models URL is the v1-base-relative path.
        call = mock_client.get.call_args
        assert call.args[0] == "https://litellm.example.com/v1/models"
        assert call.kwargs["headers"]["Authorization"].startswith("Bearer ")

    def test_health_check_marks_auth_failed_on_401(self):
        backend = self._backend()
        resp = MagicMock()
        resp.status_code = 401
        err = httpx.HTTPStatusError("unauth", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        mock_client = _mock_async_client(get_response=bad_resp)
        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=mock_client):
            health = asyncio.run(backend.health_check())

        assert health["status"] == "offline"
        assert health["details"]["auth_failed"] is True


# ---------------------------------------------------------------------------
# LLMBackendType enum
# ---------------------------------------------------------------------------
class TestLLMBackendType:
    def test_enum_includes_all_supported_backends(self):
        from app.core.llm.factory import _SUPPORTED_BACKENDS

        names = {m.value for m in LLMBackendType}
        assert "maas" in names
        assert names == _SUPPORTED_BACKENDS

    def test_enum_str_subclass_serializes_to_value(self):
        # Required for SQLAlchemy values_callable + Pydantic use_enum_values.
        assert LLMBackendType.maas == "maas"
        assert str(LLMBackendType.maas.value) == "maas"


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

    def test_maas_type_returns_maas_backend(self):
        cfg = Settings(
            llm_backend_type="maas",
            llm_maas_base_url="https://litellm.example.com/v1",
            llm_maas_model="granite-32-8b-instruct",
            llm_maas_api_key="sk-test",
        )
        backend = get_llm_backend(cfg)
        assert isinstance(backend, MaaSBackend)
        assert backend.endpoint == "https://litellm.example.com/v1"
        assert backend.default_model == "granite-32-8b-instruct"

    def test_maas_without_api_key_fails_loud(self):
        cfg = Settings(
            llm_backend_type="maas",
            llm_maas_base_url="https://litellm.example.com/v1",
            llm_maas_model="granite-32-8b-instruct",
            llm_maas_api_key=None,
        )
        with pytest.raises(LLMBackendError, match="LLM_MAAS_API_KEY"):
            get_llm_backend(cfg)

    def test_per_type_cache_keeps_multiple_backends_warm(self):
        """The per-type cache lets the runtime resolver flip between
        backends without rebuilding clients on every switch."""
        from app.core.llm.factory import get_llm_backend_for_type

        ollama_cfg = Settings(llm_backend_type="ollama")
        a = get_llm_backend_for_type("ollama", ollama_cfg)
        b = get_llm_backend_for_type("ollama", ollama_cfg)
        assert a is b  # cached

        mock_a = get_llm_backend_for_type("mock", ollama_cfg)
        mock_b = get_llm_backend_for_type("mock", ollama_cfg)
        assert mock_a is mock_b  # cached

        # Distinct types yield distinct instances — both stay hot.
        assert a is not mock_a
        # Re-fetching ollama still returns the same instance (not evicted).
        assert get_llm_backend_for_type("ollama", ollama_cfg) is a

    def test_reset_backend_cache_drops_only_named_type(self):
        from app.core.llm.factory import get_llm_backend_for_type

        cfg = Settings(llm_backend_type="ollama")
        ollama_a = get_llm_backend_for_type("ollama", cfg)
        mock_a = get_llm_backend_for_type("mock", cfg)
        reset_backend_cache("ollama")
        ollama_b = get_llm_backend_for_type("ollama", cfg)
        mock_b = get_llm_backend_for_type("mock", cfg)
        assert ollama_a is not ollama_b
        assert mock_a is mock_b  # untouched


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
            patch("app.main.get_active_backend", return_value=fake_backend),
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
            patch("app.main.get_active_backend", return_value=fake_backend),
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
            patch("app.main.get_active_backend", return_value=fake_backend),
            caplog.at_level(logging.ERROR, logger="app.main"),
        ):
            asyncio.run(_report_llm_status())

        # Even if health_check throws, the API must still come up — the
        # function returns rather than re-raising.
        msgs = [r.message for r in caplog.records]
        assert any("llm.startup.FAILED" in m for m in msgs)
        assert any("boom" in m for m in msgs)
