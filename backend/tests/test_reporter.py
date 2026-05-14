"""Unit tests for app.core.reporter — tally, parse, generate, PDF HTML."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.core.reporter import (
    ReporterError,
    WaveReporter,
    _build_pdf_html,
    _build_vm_block,
    _summarize_vm,
    _tally,
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


# ---------- PDF HTML template (weasyprint input) ----------
#
# These tests assert the HTML the renderer hands to weasyprint. We don't
# exercise weasyprint itself in CI because its runtime needs Pango/Cairo
# system libs that aren't installed on ubuntu-latest by default.


def _sample_report() -> dict:
    return {
        "wave_number": 3,
        "total_vms": 2,
        "healthy_count": 1,
        "degraded_count": 0,
        "failed_count": 1,
        "executive_summary": (
            "Wave 3 migrated 1 healthy and 1 failed VM. The failed Postgres "
            "node blocks downstream apps; escalate before continuing."
        ),
        "recommendation": "escalate",
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
                "vm_name": "db-02",
                "status": "fail",
                "summary": "port 5432 not listening",
                "findings": [{"severity": "critical", "category": "ports", "message": "5432 down"}],
                "remediation": [
                    {
                        "step": 1,
                        "action": "restart postgres",
                        "command": "systemctl restart postgresql",
                    }
                ],
            },
        ],
    }


def test_pdf_html_includes_branding_and_wave_number():
    html = _build_pdf_html(
        _sample_report(),
        generated_at=datetime(2026, 4, 28, 14, 30, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    assert "VIRTVALIDATE" in html
    assert "VM Migration Validation Platform" in html
    assert "Wave 3 — Migration Report" in html
    # logo placeholder is the .logo div
    assert 'class="logo"' in html


def test_pdf_html_renders_count_stats():
    html = _build_pdf_html(
        _sample_report(),
        generated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    assert "Total VMs" in html and "Healthy" in html and "Degraded" in html and "Failed" in html
    # the actual numbers from the sample report
    assert ">2<" in html  # total_vms
    assert ">1<" in html  # healthy + failed
    assert ">0<" in html  # degraded


def test_pdf_html_includes_recommendation_class_and_executive_summary():
    html = _build_pdf_html(
        _sample_report(),
        generated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    assert 'class="recommendation escalate"' in html
    assert "ESCALATE" in html
    assert "Wave 3 migrated 1 healthy and 1 failed VM" in html


def test_pdf_html_renders_per_vm_findings_table():
    html = _build_pdf_html(
        _sample_report(),
        generated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    # Both VMs appear with status badges
    assert "db-01" in html
    assert "db-02" in html
    assert 'class="vm-status pass"' in html
    assert 'class="vm-status fail"' in html
    # Findings table headers + content
    assert "<th>Severity</th>" in html
    assert "<th>Finding</th>" in html
    assert "<th>Remediation</th>" in html
    assert "5432 down" in html
    assert "restart postgres" in html
    assert "systemctl restart postgresql" in html


def test_pdf_html_includes_footer_metadata():
    html = _build_pdf_html(
        _sample_report(),
        generated_at=datetime(2026, 4, 28, 14, 30, 5, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    # Footer is rendered into an xhtml2pdf @frame from a body-level
    # #footer_content div, with page numbers via <pdf:pagenumber/>.
    # See PR #12 — the prior CSS string-set mechanism was weasyprint-only.
    assert "2026-04-28 14:30:05 UTC" in html
    assert "ocp-virt-prod-01" in html
    assert 'id="footer_content"' in html
    assert "<pdf:pagenumber" in html
    assert "<pdf:pagecount" in html


def test_pdf_html_escapes_user_content():
    """Hostile content from validation payload must not leak as raw HTML."""
    report = _sample_report()
    report["per_vm_findings"][0]["vm_name"] = "<evil & bad>"
    report["executive_summary"] = "<script>alert(1)</script>"
    report["per_vm_findings"][1]["findings"][0]["message"] = "<b>injected</b>"

    html = _build_pdf_html(
        report,
        generated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<evil & bad>" not in html
    assert "&lt;evil &amp; bad&gt;" in html
    assert "<b>injected</b>" not in html


def test_pdf_html_falls_back_when_vm_has_no_findings():
    report = _sample_report()
    report["per_vm_findings"] = [
        {
            "vm_id": 1,
            "vm_name": "db-01",
            "status": "pass",
            "summary": "all good",
            "findings": [],
            "remediation": [],
        }
    ]
    html = _build_pdf_html(
        report,
        generated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    assert "No findings recorded." in html


def test_build_vm_block_pads_extra_remediation_steps_into_rows():
    """Remediation steps beyond the finding count still show up as rows."""
    block = _build_vm_block(
        {
            "vm_id": 9,
            "vm_name": "app-09",
            "status": "warn",
            "summary": "minor",
            "findings": [{"severity": "warn", "category": "services", "message": "nginx warn"}],
            "remediation": [
                {"step": 1, "action": "restart nginx"},
                {"step": 2, "action": "verify upstream", "command": "curl -fsS http://app/health"},
            ],
        }
    )
    assert "restart nginx" in block
    assert "verify upstream" in block
    assert "curl -fsS http://app/health" in block
    # 1 vm-header row + 1 thead row + 2 body rows = 4 total <tr> tags.
    # (vm-header was a flex div pre-xhtml2pdf swap and contributed no <tr>;
    # the table-based replacement adds one row. See PR #12.) The 2nd body
    # row pads the severity/finding side with an em-dash so the extra
    # remediation step still renders.
    assert block.count("<tr>") == 4


def test_build_vm_block_unknown_recommendation_falls_back_to_hold():
    """Unrecognized recommendation strings shouldn't break CSS class lookup."""
    report = _sample_report()
    report["recommendation"] = "shrug"
    html = _build_pdf_html(
        report,
        generated_at=datetime(2026, 4, 28, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    assert 'class="recommendation hold"' in html


def test_build_vm_block_unrecognized_severity_uses_neutral_class():
    block = _build_vm_block(
        {
            "vm_id": 1,
            "vm_name": "x",
            "status": "warn",
            "summary": "",
            "findings": [{"severity": "moderate", "category": "x", "message": "y"}],
            "remediation": [],
        }
    )
    assert 'class="severity none"' in block


# Smoke-import the public renderer to be sure the symbol is exported.
def test_render_pdf_is_importable():
    from app.core.reporter import render_pdf  # noqa: F401

    assert callable(render_pdf)


def test_render_pdf_emits_valid_pdf_bytes():
    """End-to-end render: confirm we produce valid PDF bytes.

    Pre-xhtml2pdf migration this test couldn't exist because weasyprint
    needed Pango/Cairo system libs in CI. xhtml2pdf is pure Python, so
    the actual render path is now testable.
    """
    from app.core.reporter import render_pdf

    pdf = render_pdf(
        _sample_report(),
        generated_at=datetime(2026, 4, 28, 14, 30, 5, tzinfo=timezone.utc),
        cluster_name="ocp-virt-prod-01",
    )
    assert isinstance(pdf, bytes)
    assert pdf.startswith(b"%PDF-"), "missing PDF magic bytes"
    # 1000 is a safe floor — even the smallest valid PDF is well above this.
    assert len(pdf) > 1000, f"suspiciously small PDF ({len(pdf)} bytes)"
