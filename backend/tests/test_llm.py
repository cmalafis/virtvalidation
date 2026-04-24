"""Unit tests for app.core.llm — prompt construction, parsing, diffing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.core.llm import (
    LLMClient,
    LLMError,
    _diff_cron,
    _diff_mounts,
    _diff_network,
    _diff_ports,
    _diff_services,
)

# ---------- verdict parsing ----------


def test_parse_verdict_accepts_minimal_valid_object():
    verdict = LLMClient._parse_verdict(json.dumps({"status": "pass"}))
    assert verdict["status"] == "pass"
    assert verdict["findings"] == []
    assert verdict["remediation"] == []
    assert verdict["summary"] == ""


def test_parse_verdict_preserves_findings_and_remediation():
    raw = json.dumps(
        {
            "status": "warn",
            "summary": "cosmetic drift",
            "findings": [{"severity": "info", "category": "services", "message": "nginx missing"}],
            "remediation": [
                {"step": 1, "action": "restart nginx", "command": "systemctl start nginx"}
            ],
        }
    )
    verdict = LLMClient._parse_verdict(raw)
    assert verdict["summary"] == "cosmetic drift"
    assert verdict["findings"][0]["category"] == "services"
    assert verdict["remediation"][0]["command"] == "systemctl start nginx"


def test_parse_verdict_rejects_invalid_status():
    with pytest.raises(LLMError, match="Invalid verdict status"):
        LLMClient._parse_verdict(json.dumps({"status": "bogus"}))


def test_parse_verdict_rejects_non_object():
    with pytest.raises(LLMError, match="not a JSON object"):
        LLMClient._parse_verdict(json.dumps(["a", "list"]))


def test_parse_verdict_rejects_malformed_json():
    with pytest.raises(LLMError, match="not valid JSON"):
        LLMClient._parse_verdict("not json {")


def test_parse_verdict_rejects_findings_not_a_list():
    raw = json.dumps({"status": "pass", "findings": "oops"})
    with pytest.raises(LLMError, match="findings must be a list"):
        LLMClient._parse_verdict(raw)


# ---------- diffing ----------


def test_diff_services_detects_added_removed():
    before = [{"unit": "sshd.service"}, {"unit": "nginx.service"}]
    after = [{"unit": "sshd.service"}, {"unit": "postgresql.service"}]
    diff = _diff_services(before, after)
    assert diff["removed"] == ["nginx.service"]
    assert diff["added"] == ["postgresql.service"]


def test_diff_ports_tracks_new_and_missing():
    before = [{"proto": "tcp", "port": 22, "address": "0.0.0.0"}]
    after = [
        {"proto": "tcp", "port": 22, "address": "0.0.0.0"},
        {"proto": "tcp", "port": 443, "address": "0.0.0.0"},
    ]
    diff = _diff_ports(before, after)
    assert {"proto": "tcp", "port": 443, "address": "0.0.0.0"} in diff["added"]
    assert diff["removed"] == []


def test_diff_mounts_detects_changed_source():
    before = [{"target": "/data", "source": "/dev/sdb1", "fstype": "xfs", "options": "rw"}]
    after = [{"target": "/data", "source": "/dev/sdc1", "fstype": "xfs", "options": "rw"}]
    diff = _diff_mounts(before, after)
    assert diff["removed"] == []
    assert diff["added"] == []
    assert len(diff["changed"]) == 1
    assert diff["changed"][0]["target"] == "/data"


def test_diff_network_notes_interface_ip_changes():
    before = {
        "interfaces": {"eth0": {"ipv4": ["10.0.0.5/24"], "ipv6": []}},
        "routes": [],
        "dns": [],
    }
    after = {"interfaces": {"eth0": {"ipv4": ["10.0.0.6/24"], "ipv6": []}}, "routes": [], "dns": []}
    diff = _diff_network(before, after)
    assert diff["interfaces"]["eth0"]["ipv4_added"] == ["10.0.0.6/24"]
    assert diff["interfaces"]["eth0"]["ipv4_removed"] == ["10.0.0.5/24"]


def test_diff_cron_captures_user_and_system_changes():
    before = {
        "user_crontabs": {"root": ["0 3 * * * /opt/old.sh"]},
        "system": [{"path": "/etc/cron.d/a", "entries": ["line1"]}],
    }
    after = {
        "user_crontabs": {"root": ["0 3 * * * /opt/new.sh"]},
        "system": [{"path": "/etc/cron.d/a", "entries": ["line1", "line2"]}],
    }
    diff = _diff_cron(before, after)
    assert diff["users"]["root"]["added"] == ["0 3 * * * /opt/new.sh"]
    assert diff["users"]["root"]["removed"] == ["0 3 * * * /opt/old.sh"]
    assert diff["system"]["/etc/cron.d/a"]["added"] == ["line2"]


# ---------- end-to-end validate() with mocked httpx ----------


def _build_mock_httpx_client(response_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=response_body)
    mock_client = MagicMock()
    mock_client.post = MagicMock(return_value=resp)
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    return mock_client


def test_validate_sends_correct_payload_and_parses_verdict(mock_ollama_verdict):
    canned = {"message": {"content": json.dumps(mock_ollama_verdict)}}
    mock_client = _build_mock_httpx_client(canned)

    with patch("app.core.llm.httpx.Client", return_value=mock_client):
        client = LLMClient(host="http://mock:11434", model="test-model")
        result = client.validate(
            baseline={"services": [{"unit": "sshd.service"}], "ports": []},
            current_state={"services": [{"unit": "sshd.service"}], "ports": []},
            vm_role="web",
        )

    # Correct URL
    call = mock_client.post.call_args
    assert call.args[0] == "http://mock:11434/api/chat"

    payload = call.kwargs["json"]
    assert payload["model"] == "test-model"
    assert payload["stream"] is False
    assert payload["format"] == "json"
    assert payload["options"]["temperature"] == 0.1
    assert len(payload["messages"]) == 2
    assert payload["messages"][0]["role"] == "system"
    assert "VirtValidate" in payload["messages"][0]["content"]
    assert payload["messages"][1]["role"] == "user"
    assert "web" in payload["messages"][1]["content"]

    # The verdict is enriched with a computed diff
    assert result["status"] == "pass"
    assert "diff" in result
    assert "services" in result["diff"]


def test_validate_raises_on_http_error():
    import httpx

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post = MagicMock(side_effect=httpx.ConnectError("refused"))

    with patch("app.core.llm.httpx.Client", return_value=mock_client):
        client = LLMClient()
        with pytest.raises(LLMError, match="Ollama request failed"):
            client.validate({}, {}, "web")


def test_validate_raises_when_response_content_empty():
    canned = {"message": {"content": ""}}
    mock_client = _build_mock_httpx_client(canned)

    with patch("app.core.llm.httpx.Client", return_value=mock_client):
        client = LLMClient()
        with pytest.raises(LLMError, match="empty message"):
            client.validate({}, {}, "web")
