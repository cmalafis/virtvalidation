"""Tests for /api/settings, /api/system/*, /api/health/* endpoints.

LLM-touching health/system tests stub the backend via the factory cache
rather than patching httpx — the multi-backend refactor means the
endpoint code no longer imports httpx directly.
"""

from __future__ import annotations

import pytest

from app.core.llm import factory as llm_factory
from app.core.llm.base import LLMBackend


class _StubBackend(LLMBackend):
    """Minimal in-memory backend for endpoint tests."""

    backend_type = "stub"

    def __init__(
        self,
        models: list[str] | None = None,
        health: dict | None = None,
        list_models_raises: bool = False,
    ):
        self.default_model = "stub-model"
        self.endpoint = "stub://"
        self._models = models if models is not None else ["stub-model"]
        self._health = health
        self._list_models_raises = list_models_raises

    async def chat(
        self, messages, model=None, temperature=0.1, max_tokens=None
    ):  # pragma: no cover
        return {"content": "{}", "model": self.default_model}

    async def chat_stream(self, messages, model=None, temperature=0.1):  # pragma: no cover
        raise NotImplementedError
        if False:
            yield ""

    async def health_check(self):
        if self._health is not None:
            return self._health
        return {
            "status": "online",
            "backend": self.backend_type,
            "model": self.default_model,
            "endpoint": self.endpoint,
            "latency_ms": 1,
            "details": {"available_models": self._models},
        }

    def list_models(self):
        if self._list_models_raises:
            raise RuntimeError("boom")
        return list(self._models)


@pytest.fixture
def stub_backend(monkeypatch):
    """Inject a stub backend by replacing ``get_llm_backend`` directly.

    The factory's cache check keys on the configured backend_type, so
    just shoving a stub into the cache isn't enough — the next call
    sees the mismatch and rebuilds an Ollama backend. Patching the
    factory function itself is the simplest hammer that works.
    """
    state: dict = {"backend": None}

    def _install(**kwargs) -> _StubBackend:
        b = _StubBackend(**kwargs)
        state["backend"] = b
        return b

    def _fake_get(cfg=None):
        if state["backend"] is None:
            state["backend"] = _StubBackend()
        return state["backend"]

    # Patch every import path that resolves the factory.
    monkeypatch.setattr("app.core.llm.factory.get_llm_backend", _fake_get)
    monkeypatch.setattr("app.api.health.get_llm_backend", _fake_get)
    monkeypatch.setattr("app.api.settings.get_llm_backend", _fake_get)
    yield _install
    llm_factory.reset_backend_cache()


# ---------- Settings CRUD ----------


def test_get_settings_creates_default_row(client):
    r = client.get("/api/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 1
    assert body["ollama_model"] == "llama3:8b"
    assert body["schedule_preset"] == "twice_daily"
    assert "updated_at" in body


def test_put_settings_persists_and_returns_updated_row(client):
    client.get("/api/settings")  # ensure row exists
    r = client.put(
        "/api/settings", json={"schedule_preset": "hourly", "ollama_model": "llama3:70b"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["schedule_preset"] == "hourly"
    assert body["ollama_model"] == "llama3:70b"

    again = client.get("/api/settings").json()
    assert again["schedule_preset"] == "hourly"
    assert again["ollama_model"] == "llama3:70b"


def test_put_settings_validates_schedule_preset(client):
    r = client.put("/api/settings", json={"schedule_preset": "monthly"})
    assert r.status_code == 422


def test_put_settings_partial_update_keeps_other_fields(client):
    client.put(
        "/api/settings", json={"ollama_model": "mistral:latest", "schedule_preset": "once_daily"}
    )
    r = client.put("/api/settings", json={"schedule_preset": "twice_daily"})
    body = r.json()
    assert body["ollama_model"] == "mistral:latest"
    assert body["schedule_preset"] == "twice_daily"


# ---------- Health endpoints ----------


def test_postgres_health_reports_online_for_test_db(client):
    r = client.get("/api/health/postgres")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "online"
    assert "error" not in body


def test_llm_health_reports_offline_when_backend_offline(client, stub_backend):
    stub_backend(
        health={
            "status": "offline",
            "backend": "stub",
            "model": "stub-model",
            "endpoint": "stub://",
            "latency_ms": -1,
            "details": {"error": "refused"},
        }
    )
    r = client.get("/api/health/llm")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "offline"
    assert body["details"]["error"] == "refused"


def test_llm_health_reports_online_when_backend_reachable(client, stub_backend):
    stub_backend(models=["llama3:8b", "mistral:latest"])
    r = client.get("/api/health/llm")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "online"
    assert body["backend"] == "stub"
    assert body["model"] == "stub-model"
    assert body["details"]["available_models"] == ["llama3:8b", "mistral:latest"]


def test_ollama_health_alias_routes_through_backend(client, stub_backend):
    """The legacy /api/health/ollama path stays alive for old UI builds."""
    stub_backend(models=["llama3:8b"])
    r = client.get("/api/health/ollama")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "online"


def test_full_health_aggregates_dependencies(client, stub_backend):
    stub_backend(models=["llama3:8b"])
    r = client.get("/api/health/full")

    assert r.status_code == 200
    body = r.json()
    assert body["api"]["status"] == "online"
    assert body["api"]["version"]
    # New canonical shape — components.{database,llm}
    assert body["components"]["llm"]["status"] == "online"
    assert body["components"]["database"]["status"] == "online"
    # Back-compat keys still populated for the old dashboard build.
    assert body["ollama"]["status"] == "online"
    assert body["postgres"]["status"] == "online"


# ---------- System endpoints ----------


def test_ssh_public_key_returns_404_when_missing(client, tmp_path, monkeypatch):
    # Point at a non-existent path so neither .pub nor private file exists
    monkeypatch.setattr("app.api.settings.app_config.ssh_key_path", str(tmp_path / "nope"))
    r = client.get("/api/system/ssh-public-key")
    assert r.status_code == 404
    assert "No SSH key found" in r.json()["detail"]


def test_ssh_public_key_reads_pub_file_when_present(client, tmp_path, monkeypatch):
    priv = tmp_path / "id_ed25519"
    priv.write_text("PRIVATE KEY CONTENTS — should NEVER be returned")
    pub = tmp_path / "id_ed25519.pub"
    pub.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample virtvalidate@appliance\n")

    monkeypatch.setattr("app.api.settings.app_config.ssh_key_path", str(priv))

    r = client.get("/api/system/ssh-public-key")
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "ssh-ed25519"
    assert body["public_key"].startswith("ssh-ed25519 ")
    assert "PRIVATE" not in body["public_key"]
    assert body["fingerprint"] is not None
    assert body["fingerprint"].startswith("SHA256:")


def test_ollama_models_lists_active_backend_models(client, stub_backend):
    """Endpoint name predates the multi-backend refactor; the response is
    now whatever ``backend.list_models()`` returns wrapped as
    ``{"name": <id>}`` so the existing UI dropdown still renders."""
    stub_backend(models=["llama3:8b", "mistral:latest"])
    r = client.get("/api/system/ollama-models")
    assert r.status_code == 200
    body = r.json()
    assert [m["name"] for m in body["models"]] == ["llama3:8b", "mistral:latest"]


def test_ollama_models_returns_empty_when_backend_unreachable(client, stub_backend):
    """list_models() degrades gracefully — backend impls swallow transport
    errors and return ``[]``. The endpoint reflects that as an empty list."""
    stub_backend(models=[])
    r = client.get("/api/system/ollama-models")
    assert r.status_code == 200
    assert r.json() == {"models": []}


def test_llm_info_returns_config_and_health(client, stub_backend):
    stub_backend(models=["llama3:8b"])
    r = client.get("/api/system/llm-info")
    assert r.status_code == 200
    body = r.json()
    assert body["config"]["backend"] == "stub"
    assert body["config"]["model"] == "stub-model"
    assert body["config"]["endpoint"] == "stub://"
    assert body["health"]["status"] == "online"
    assert body["health"]["backend"] == "stub"
