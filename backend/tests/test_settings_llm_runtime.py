"""Runtime LLM backend selection — DB-backed, switchable from Settings UI.

Covers:
  - The TTL cache + invalidation contract on get_active_backend_type.
  - is_backend_configured / missing_config_for sense-checks.
  - GET /api/settings/llm shape.
  - PUT /api/settings/llm — happy path, invalidates the cache,
    refuses unconfigured backends with 422.
  - POST /api/settings/llm/test-connection — mock returns trivially ok,
    a 401 from a real backend marks reachable=true authenticated=false.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core.config import Settings
from app.core.llm import runtime as runtime_mod
from app.core.llm.factory import reset_backend_cache
from app.core.llm.runtime import (
    get_active_backend_type,
    is_backend_configured,
    missing_config_for,
)
from app.core.llm.runtime import (
    invalidate as invalidate_runtime_cache,
)

# Alias so pytest doesn't collect the runtime helper as a test function.
from app.core.llm.runtime import test_connection as run_connection_test


@pytest.fixture(autouse=True)
def _reset_caches():
    reset_backend_cache()
    invalidate_runtime_cache()
    yield
    reset_backend_cache()
    invalidate_runtime_cache()


# ---------------------------------------------------------------------------
# is_backend_configured / missing_config_for
# ---------------------------------------------------------------------------
class TestBackendConfigured:
    def test_mock_always_configured(self):
        assert is_backend_configured("mock", Settings()) is True
        assert missing_config_for("mock", Settings()) == []

    def test_ollama_configured_when_host_set(self):
        cfg = Settings(ollama_host="http://ollama:11434")
        assert is_backend_configured("ollama", cfg) is True

    def test_kserve_requires_endpoint_and_model(self):
        cfg = Settings(kserve_endpoint=None, kserve_model_name=None)
        assert is_backend_configured("kserve", cfg) is False
        missing = missing_config_for("kserve", cfg)
        assert "KSERVE_ENDPOINT" in missing
        assert "KSERVE_MODEL_NAME" in missing

    def test_maas_requires_url_model_and_key(self):
        cfg = Settings(llm_maas_base_url=None, llm_maas_model=None, llm_maas_api_key=None)
        assert is_backend_configured("maas", cfg) is False
        missing = missing_config_for("maas", cfg)
        assert missing == ["LLM_MAAS_BASE_URL", "LLM_MAAS_MODEL", "LLM_MAAS_API_KEY"]

    def test_maas_partially_configured_lists_only_missing(self):
        cfg = Settings(
            llm_maas_base_url="https://x/v1",
            llm_maas_model="granite",
            llm_maas_api_key=None,
        )
        assert is_backend_configured("maas", cfg) is False
        assert missing_config_for("maas", cfg) == ["LLM_MAAS_API_KEY"]

    def test_unknown_backend_not_configured(self):
        assert is_backend_configured("not-a-real-thing", Settings()) is False


# ---------------------------------------------------------------------------
# TTL cache for the active-backend type
# ---------------------------------------------------------------------------
class TestActiveBackendTypeCache:
    def test_falls_back_to_env_var_when_table_missing(self):
        # Conftest's in-memory DB doesn't have app_settings unless the
        # `engine` fixture creates the schema. Without it, the resolver
        # must not raise — it returns the bootstrap value.
        invalidate_runtime_cache()
        bt = get_active_backend_type()
        # The conftest sets OLLAMA_HOST/MODEL but not LLM_BACKEND_TYPE,
        # so default is "ollama".
        assert bt in {"ollama", "mock"}

    def test_invalidate_drops_the_cache(self, monkeypatch):
        calls: list[int] = []

        def _fake_read(db=None):
            calls.append(1)
            return "mock"

        monkeypatch.setattr(runtime_mod, "_read_active_backend_from_db", _fake_read)
        invalidate_runtime_cache()
        get_active_backend_type()
        get_active_backend_type()  # should be cached
        assert len(calls) == 1
        invalidate_runtime_cache()
        get_active_backend_type()
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------
class TestConnectionTest:
    def test_mock_returns_trivially_ok(self):
        import asyncio

        result = asyncio.run(run_connection_test("mock"))
        assert result.reachable is True
        assert result.authenticated is True
        assert result.model_available is True
        assert result.error is None

    def test_unconfigured_backend_returns_clear_error(self):
        import asyncio

        # vllm with no endpoint → unconfigured.
        result = asyncio.run(run_connection_test("vllm"))
        assert result.reachable is False
        assert result.authenticated is False
        assert "not configured" in (result.error or "").lower()
        # Key never appears in the error payload (defense-in-depth: the
        # field doesn't apply to vllm but the contract holds).
        assert "API_KEY" not in (result.error or "") or "LLM_MAAS_API_KEY" in (result.error or "")

    def test_maas_401_marks_reachable_but_unauthenticated(self, monkeypatch):
        """Hitting an endpoint that returns 401 = reachable, auth failed.
        The Settings UI uses this distinction to tell the operator
        "the endpoint works, your key is wrong" vs "the endpoint is
        down"."""
        import asyncio

        # Configure MaaS
        monkeypatch.setattr(
            runtime_mod._module_settings, "llm_maas_base_url", "https://litellm.example.com/v1"
        )
        monkeypatch.setattr(
            runtime_mod._module_settings, "llm_maas_model", "granite-32-8b-instruct"
        )
        monkeypatch.setattr(
            runtime_mod._module_settings, "llm_maas_api_key", "sk-leak-test-XXXXXXXX"
        )
        reset_backend_cache()

        # Make the MaaS health_check return a 401-marked offline.
        resp = MagicMock()
        resp.status_code = 401
        err = httpx.HTTPStatusError("unauth", request=MagicMock(), response=resp)
        bad_resp = MagicMock()
        bad_resp.raise_for_status = MagicMock(side_effect=err)
        instance = MagicMock()
        instance.get = AsyncMock(return_value=bad_resp)
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)

        with patch("app.core.llm.maas_backend.httpx.AsyncClient", return_value=instance):
            result = asyncio.run(run_connection_test("maas"))

        assert result.reachable is True, "401 means the endpoint answered"
        assert result.authenticated is False
        assert result.model_available is False
        assert "auth" in (result.error or "").lower()
        # Key absolutely must not leak into the error string.
        assert "sk-leak-test-XXXXXXXX" not in (result.error or "")


# ---------------------------------------------------------------------------
# Settings API endpoints
# ---------------------------------------------------------------------------
class TestSettingsLLMEndpoints:
    def test_get_llm_settings_returns_active_and_options(self, client):
        r = client.get("/api/settings/llm")
        assert r.status_code == 200
        body = r.json()
        assert "active_llm_backend" in body
        assert isinstance(body["available_backends"], list)
        # Mock is always present and badged dev_only.
        mock_opt = next(o for o in body["available_backends"] if o["type"] == "mock")
        assert mock_opt["dev_only"] is True
        assert mock_opt["configured"] is True
        # MaaS should be present (whether configured depends on env).
        assert any(o["type"] == "maas" for o in body["available_backends"])
        # last_llm_error null on a fresh row.
        assert body.get("last_llm_error") is None

    def test_put_llm_settings_switches_active_backend(self, client):
        # Switch to mock (always configured).
        r = client.put("/api/settings/llm", json={"active_llm_backend": "mock"})
        assert r.status_code == 200, r.text
        assert r.json()["active_llm_backend"] == "mock"
        # GET reflects the change.
        r2 = client.get("/api/settings/llm")
        assert r2.json()["active_llm_backend"] == "mock"

    def test_put_llm_settings_rejects_unconfigured_with_422(self, client):
        # vllm has no endpoint configured by default in tests.
        r = client.put("/api/settings/llm", json={"active_llm_backend": "vllm"})
        assert r.status_code == 422
        assert "not configured" in r.json()["detail"].lower()
        assert "VLLM_ENDPOINT" in r.json()["detail"]

    def test_test_connection_endpoint_for_mock(self, client):
        r = client.post(
            "/api/settings/llm/test-connection",
            json={"backend_type": "mock"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["backend_type"] == "mock"
        assert body["reachable"] is True
        assert body["authenticated"] is True
        assert body["model_available"] is True
        assert body["error"] is None

    def test_test_connection_clears_last_llm_error_on_success(self, client, db_session):
        from app.models.settings import AppSettings

        # Seed a stale error on the singleton row.
        row = AppSettings(id=1, last_llm_error="MaaS auth failed")
        db_session.add(row)
        db_session.commit()

        r = client.post(
            "/api/settings/llm/test-connection",
            json={"backend_type": "mock"},
        )
        assert r.status_code == 200

        # Reload — last_llm_error should now be cleared.
        db_session.expire_all()
        row = db_session.get(AppSettings, 1)
        assert row.last_llm_error is None
        assert row.last_llm_error_at is None
