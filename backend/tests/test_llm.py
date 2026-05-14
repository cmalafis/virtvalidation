"""Unit tests for app.core.llm — prompt construction, parsing, diffing.

The LLMClient now delegates transport to an LLMBackend instance, so the
end-to-end tests inject a stub backend instead of patching httpx. Per-
backend HTTP behavior lives in test_llm_backends.py.
"""

from __future__ import annotations

import json

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
from app.core.llm.base import LLMBackend, LLMBackendError


# ---------------------------------------------------------------------------
# Stub backend — captures the messages LLMClient sends and returns canned
# responses. Tests assert on what the orchestrator asked for and what it
# made of the response.
# ---------------------------------------------------------------------------
class StubBackend(LLMBackend):
    backend_type = "stub"

    def __init__(self, response: dict | None = None, raise_on_call: Exception | None = None):
        self.default_model = "stub-model"
        self.endpoint = "stub://"
        self._response = response or {}
        self._raise = raise_on_call
        self.calls: list[dict] = []

    async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }
        )
        if self._raise is not None:
            raise self._raise
        return self._response

    async def chat_stream(self, messages, model=None, temperature=0.1):  # pragma: no cover
        raise NotImplementedError
        if False:
            yield ""

    async def health_check(self):  # pragma: no cover
        return {"status": "online", "backend": self.backend_type, "latency_ms": 0}

    def list_models(self):  # pragma: no cover
        return [self.default_model]


# ---------- verdict parsing ----------


def test_parse_verdict_accepts_minimal_valid_object():
    verdict = LLMClient._parse_verdict(json.dumps({"status": "pass"}))
    assert verdict["status"] == "pass"
    assert verdict["findings"] == []
    assert verdict["remediation"] == []
    assert verdict["summary"] == ""


def test_parse_verdict_preserves_findings_and_remediation():
    """Validation v0.x tightened the schema: every finding must carry
    source_evidence / current_evidence / remediation / confidence."""
    raw = json.dumps(
        {
            "status": "warn",
            "summary": "cosmetic drift",
            "findings": [
                {
                    "severity": "info",
                    "category": "services",
                    "title": "nginx missing",
                    "source_evidence": "nginx.service active in baseline",
                    "current_evidence": "nginx.service absent in current",
                    "remediation": "systemctl start nginx",
                    "confidence": "medium",
                }
            ],
            "remediation": [
                {"step": 1, "action": "restart nginx", "command": "systemctl start nginx"}
            ],
        }
    )
    verdict = LLMClient._parse_verdict(raw)
    assert verdict["summary"] == "cosmetic drift"
    assert verdict["findings"][0]["category"] == "services"
    assert verdict["remediation"][0]["command"] == "systemctl start nginx"


def test_parse_verdict_requires_evidence_fields_per_finding():
    """Findings missing source_evidence / current_evidence / remediation
    are rejected so the retry loop can re-prompt the LLM."""
    raw = json.dumps(
        {
            "status": "warn",
            "findings": [{"severity": "low", "title": "x", "confidence": "medium"}],
        }
    )
    with pytest.raises(LLMError, match="source_evidence"):
        LLMClient._parse_verdict(raw)


def test_parse_verdict_enforces_status_severity_consistency():
    """A 'pass' verdict with a critical finding is internally
    inconsistent — the parser rejects so the retry loop can correct."""
    raw = json.dumps(
        {
            "status": "pass",
            "findings": [
                {
                    "severity": "critical",
                    "title": "DB down",
                    "source_evidence": "x",
                    "current_evidence": "y",
                    "remediation": "z",
                    "confidence": "high",
                }
            ],
        }
    )
    with pytest.raises(LLMError, match="must be 'fail'"):
        LLMClient._parse_verdict(raw)


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


# ---------- end-to-end validate() with stub backend ----------


def test_validate_sends_correct_messages_and_parses_verdict(mock_ollama_verdict):
    stub = StubBackend(response={"content": json.dumps(mock_ollama_verdict), "model": "stub-model"})
    client = LLMClient(backend=stub)

    result = client.validate(
        baseline={"services": [{"unit": "sshd.service"}], "ports": []},
        current_state={"services": [{"unit": "sshd.service"}], "ports": []},
        vm_role="web",
    )

    # The orchestrator sent system + user messages with the right shape.
    assert len(stub.calls) == 1
    sent = stub.calls[0]
    assert sent["temperature"] == 0.1
    assert len(sent["messages"]) == 2
    assert sent["messages"][0]["role"] == "system"
    assert "VirtValidate" in sent["messages"][0]["content"]
    assert sent["messages"][1]["role"] == "user"
    assert "web" in sent["messages"][1]["content"]

    # The verdict is enriched with a computed diff.
    assert result["status"] == "pass"
    assert "diff" in result
    assert "services" in result["diff"]


def test_validate_raises_on_backend_error():
    stub = StubBackend(raise_on_call=LLMBackendError("backend offline"))
    client = LLMClient(backend=stub)

    with pytest.raises(LLMError, match="backend offline"):
        client.validate({}, {}, "web")


def test_validate_returns_manual_review_when_retries_exhausted():
    """Behavior change from v0.x retry loop: when the LLM produces
    unparseable content twice in a row (initial + 1 retry), the
    pipeline returns a 'manual review needed' verdict instead of
    raising so the bulk path can skip the bad VM without aborting
    every other one."""
    stub = StubBackend(response={"content": "", "model": "stub-model"})
    client = LLMClient(backend=stub)

    verdict = client.validate({}, {}, "web")
    assert verdict["needs_manual_review"] is True
    assert verdict["status"] == "warn"
    # diff still attached so the UI can render the raw signal.
    assert "diff" in verdict
    # Stub was hit twice: initial attempt + 1 retry.
    assert len(stub.calls) == 2


def test_validate_raises_only_on_backend_transport_error():
    """LLMBackendError (transport-layer) is still fatal — the retry
    loop only swallows structural validation failures."""
    stub = StubBackend(raise_on_call=LLMBackendError("backend offline"))
    client = LLMClient(backend=stub)
    with pytest.raises(LLMError, match="backend offline"):
        client.validate({}, {}, "web")
