"""Tests for /api/settings, /api/system/*, /api/health/* endpoints."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

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


def test_ollama_health_reports_offline_when_unreachable(client):
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(side_effect=httpx.ConnectError("refused"))

    with patch("app.api.health.httpx.Client", return_value=mock_client):
        r = client.get("/api/health/ollama")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "offline"
    assert "refused" in (body.get("error") or "")


def test_ollama_health_reports_online_when_reachable(client):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(
        return_value={
            "models": [
                {"name": "llama3:8b"},
                {"name": "mistral:latest"},
            ]
        }
    )

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=resp)

    with patch("app.api.health.httpx.Client", return_value=mock_client):
        r = client.get("/api/health/ollama")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "online"
    assert body["model"] == "llama3:8b"
    assert body["available_models"] == ["llama3:8b", "mistral:latest"]


def test_full_health_aggregates_dependencies(client):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={"models": [{"name": "llama3:8b"}]})

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=resp)

    with patch("app.api.health.httpx.Client", return_value=mock_client):
        r = client.get("/api/health/full")

    assert r.status_code == 200
    body = r.json()
    assert body["api"]["status"] == "online"
    assert body["api"]["version"]
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


def test_ollama_models_proxies_tags_response(client):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(
        return_value={
            "models": [
                {"name": "llama3:8b", "size": 4_700_000_000, "modified_at": "2026-04-21T00:00:00Z"},
                {
                    "name": "mistral:latest",
                    "size": 4_100_000_000,
                    "modified_at": "2026-04-22T00:00:00Z",
                },
            ]
        }
    )

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=resp)

    with patch("app.api.settings.httpx.Client", return_value=mock_client):
        r = client.get("/api/system/ollama-models")

    assert r.status_code == 200
    body = r.json()
    assert len(body["models"]) == 2
    assert body["models"][0]["name"] == "llama3:8b"


def test_ollama_models_returns_502_when_unreachable(client):
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(side_effect=httpx.ConnectError("refused"))

    with patch("app.api.settings.httpx.Client", return_value=mock_client):
        r = client.get("/api/system/ollama-models")

    assert r.status_code == 502
    assert "Ollama unavailable" in r.json()["detail"]
