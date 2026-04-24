"""Unit tests for app.core.reporter — tally, parse, generate, PDF render."""

from __future__ import annotations

import json

import pytest

from app.core.reporter import (
    ReporterError,
    WaveReporter,
    _summarize_vm,
    _tally,
    render_pdf,
)

# ---------- counting ----------


def test_tally_counts_all_statuses():
    results = [
        {"status": "pass"},
        {"status": "pass"},
        {"status": "warn"},
        {"status": "fail"},
    ]
    assert _tally(results) == {"healthy": 2, "degraded": 1, "failed": 1}


def test_tally_ignores_unrecognized_status():
    results = [{"status": "pass"}, {"status": "bogus"}, {}]
    assert _tally(results) == {"healthy": 1, "degraded": 0, "failed": 0}


def test_tally_empty_input():
    assert _tally([]) == {"healthy": 0, "degraded": 0, "failed": 0}


# ---------- per-VM summary shape ----------


def test_summarize_vm_fills_defaults_for_missing_fields():
    summary = _summarize_vm({"vm_id": 1, "status": "pass"})
    assert summary == {
        "vm_id": 1,
        "vm_name": "",
        "status": "pass",
        "summary": "",
        "findings": [],
        "remediation": [],
    }


def test_summarize_vm_passes_through_all_fields():
    summary = _summarize_vm(
        {
            "vm_id": 7,
            "vm_name": "web-07",
            "status": "warn",
            "summary": "minor drift",
            "findings": [{"severity": "info", "category": "ports", "message": "new port"}],
            "remediation": [{"step": 1, "action": "monitor"}],
        }
    )
    assert summary["vm_name"] == "web-07"
    assert summary["findings"][0]["category"] == "ports"
    assert summary["remediation"][0]["step"] == 1


# ---------- response validation ----------


def test_parse_accepts_valid():
    raw = json.dumps({"executive_summary": "Wave 1 migrated cleanly.", "recommendation": "proceed"})
    parsed = WaveReporter._parse(raw)
    assert parsed["executive_summary"] == "Wave 1 migrated cleanly."
    assert parsed["recommendation"] == "proceed"


def test_parse_rejects_invalid_recommendation():
    raw = json.dumps({"executive_summary": "...", "recommendation": "abort"})
    with pytest.raises(ReporterError, match="recommendation"):
        WaveReporter._parse(raw)


def test_parse_rejects_empty_summary():
    raw = json.dumps({"executive_summary": "   ", "recommendation": "proceed"})
    with pytest.raises(ReporterError, match="executive_summary"):
        WaveReporter._parse(raw)


def test_parse_rejects_malformed_json():
    with pytest.raises(ReporterError, match="not valid JSON"):
        WaveReporter._parse("{not json")


# ---------- end-to-end generate() ----------


def test_generate_assembles_structured_report(monkeypatch):
    wave = {
        "wave_number": 1,
        "vm_ids": [1, 2, 3],
        "rationale": "DB tier",
        "estimated_risk": "high",
    }
    verdicts = [
        {
            "vm_id": 1,
            "vm_name": "db-01",
            "status": "pass",
            "summary": "healthy",
            "findings": [],
            "remediation": [],
        },
        {
            "vm_id": 2,
            "vm_name": "db-02",
            "status": "warn",
            "summary": "cosmetic drift",
            "findings": [{"severity": "warn", "category": "services", "message": "nginx missing"}],
            "remediation": [],
        },
        {
            "vm_id": 3,
            "vm_name": "db-03",
            "status": "fail",
            "summary": "port 5432 not listening",
            "findings": [{"severity": "critical", "category": "ports", "message": "5432 down"}],
            "remediation": [{"step": 1, "action": "restart postgres", "command": None}],
        },
    ]

    canned = json.dumps(
        {
            "executive_summary": (
                "Wave 1 migrated 1 healthy, 1 degraded, and 1 failed VM. "
                "The failed Postgres server blocks downstream app tiers; "
                "escalate before proceeding."
            ),
            "recommendation": "escalate",
        }
    )
    reporter = WaveReporter()
    monkeypatch.setattr(reporter, "_chat", lambda system, user: canned)

    report = reporter.generate(wave, verdicts)

    assert report["wave_number"] == 1
    assert report["total_vms"] == 3
    assert report["healthy_count"] == 1
    assert report["degraded_count"] == 1
    assert report["failed_count"] == 1
    assert report["recommendation"] == "escalate"
    assert "Wave 1 migrated" in report["executive_summary"]
    assert len(report["per_vm_findings"]) == 3
    assert report["per_vm_findings"][2]["status"] == "fail"


def test_generate_passes_counts_and_rationale_to_prompt(monkeypatch):
    """Verify the planner's rationale and tallied counts make it into the LLM prompt."""
    captured = {}

    def fake_chat(system, user):
        captured["user"] = user
        return json.dumps({"executive_summary": "ok", "recommendation": "proceed"})

    reporter = WaveReporter()
    monkeypatch.setattr(reporter, "_chat", fake_chat)

    wave = {
        "wave_number": 2,
        "vm_ids": [10],
        "rationale": "Apps after DB",
        "estimated_risk": "medium",
    }
    reporter.generate(
        wave,
        [
            {
                "vm_id": 10,
                "vm_name": "app-10",
                "status": "pass",
                "summary": "ok",
                "findings": [],
                "remediation": [],
            }
        ],
    )

    assert "Wave 2" in captured["user"]
    assert "Apps after DB" in captured["user"]
    assert "healthy=1" in captured["user"]
    assert "medium" in captured["user"]


# ---------- PDF rendering ----------


def test_render_pdf_produces_valid_pdf_bytes():
    report = {
        "wave_number": 1,
        "total_vms": 2,
        "healthy_count": 1,
        "degraded_count": 1,
        "failed_count": 0,
        "executive_summary": "All clear except one cosmetic service drift.",
        "recommendation": "proceed",
        "per_vm_findings": [
            {
                "vm_id": 1,
                "vm_name": "db-01",
                "status": "pass",
                "summary": "healthy",
                "findings": [],
                "remediation": [],
            },
            {
                "vm_id": 2,
                "vm_name": "app-01",
                "status": "warn",
                "summary": "nginx restart needed",
                "findings": [
                    {"severity": "warn", "category": "services", "message": "nginx absent"}
                ],
                "remediation": [],
            },
        ],
    }
    pdf = render_pdf(report)
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF")
    assert b"%%EOF" in pdf[-32:]
    assert len(pdf) > 500


def test_render_pdf_escapes_html_in_vm_names():
    """Unescaped <br/> in a VM name would break reportlab's Paragraph XML."""
    report = {
        "wave_number": 1,
        "total_vms": 1,
        "healthy_count": 0,
        "degraded_count": 0,
        "failed_count": 1,
        "executive_summary": "Testing <script>alert(1)</script> escape.",
        "recommendation": "hold",
        "per_vm_findings": [
            {
                "vm_id": 1,
                "vm_name": "<evil & bad>",
                "status": "fail",
                "summary": "bad & wrong",
                "findings": [
                    {"severity": "critical", "category": "other", "message": "<b>injected</b>"}
                ],
                "remediation": [],
            }
        ],
    }
    # Should not raise — escaping protects reportlab's XML parser.
    pdf = render_pdf(report)
    assert pdf.startswith(b"%PDF")
