"""
Wave report generator.

Aggregates per-VM validation verdicts for a single migration wave and asks the
local Ollama model for a CISO-level executive summary, then returns a structured
WaveReport plus a PDF rendering.
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Literal

import httpx
from reportlab.lib import colors
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.core.config import settings

Recommendation = Literal["proceed", "hold", "escalate"]
_ALLOWED_RECOMMENDATIONS: set[str] = {"proceed", "hold", "escalate"}

REPORTER_SYSTEM_PROMPT = """You are VirtValidate, writing a migration wave report
for a CISO. The reader is non-technical but risk-aware — think regulated
enterprise, audit-ready language.

You will receive:
  - the wave number and the VMs in it
  - each VM's post-migration verdict ("pass" | "warn" | "fail"), findings,
    and remediation steps from the validation engine

Write a SINGLE paragraph (4-6 sentences) executive summary that:
  - states how the wave went overall, concretely (how many healthy / degraded
    / failed, and which roles/services are affected)
  - names the specific risks that matter at CISO level (data integrity,
    availability, compliance, auth/identity, audit logging)
  - ends with a clear recommendation to proceed, hold, or escalate

Also return a recommendation keyword separately:
  - "proceed": all VMs pass or only low-severity warnings; next wave can begin
  - "hold": one or more warn/fail states that need operator attention before
    continuing, but nothing is actively on fire
  - "escalate": failed VMs affecting stateful services, auth, or compliance
    controls — needs leadership / incident response attention

Respond with a SINGLE JSON object and nothing else:
{
  "executive_summary": "the paragraph",
  "recommendation": "proceed" | "hold" | "escalate"
}
No markdown, no preamble."""


class ReporterError(RuntimeError):
    """Raised when the report LLM call fails or returns unusable output."""


class WaveReporter:
    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout: float = 180.0,
    ):
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout

    def generate(self, wave: dict, validation_results: list[dict]) -> dict:
        """Build a structured WaveReport for a single wave.

        wave: {"wave_number": int, "vm_ids": [...], "rationale": str,
               "estimated_risk": "low"|"medium"|"high"}
        validation_results: list of per-VM verdicts, each with at least
          "vm_id", "vm_name", "status", "summary", "findings", "remediation".
        """
        counts = _tally(validation_results)
        per_vm_findings = [_summarize_vm(v) for v in validation_results]

        prompt = self._render_prompt(wave, validation_results, counts)
        raw = self._chat(REPORTER_SYSTEM_PROMPT, prompt)
        llm = self._parse(raw)

        return {
            "wave_number": wave["wave_number"],
            "total_vms": len(validation_results),
            "healthy_count": counts["healthy"],
            "degraded_count": counts["degraded"],
            "failed_count": counts["failed"],
            "executive_summary": llm["executive_summary"],
            "per_vm_findings": per_vm_findings,
            "recommendation": llm["recommendation"],
        }

    def _chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.post(f"{self.host}/api/chat", json=payload)
                resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ReporterError(f"Ollama request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise ReporterError(f"Ollama returned non-JSON envelope: {e}") from e

        content = (body.get("message") or {}).get("content", "")
        if not content:
            raise ReporterError("Ollama returned an empty message")
        return content

    @staticmethod
    def _render_prompt(wave: dict, validation_results: list[dict], counts: dict[str, int]) -> str:
        return (
            f"Wave {wave['wave_number']} "
            f"(estimated_risk={wave.get('estimated_risk', 'unknown')})\n"
            f"Rationale from planner: {wave.get('rationale', '')}\n\n"
            f"Totals — healthy={counts['healthy']}, degraded={counts['degraded']}, "
            f"failed={counts['failed']}\n\n"
            f"Per-VM verdicts:\n"
            f"{json.dumps(validation_results, indent=2, sort_keys=True, default=str)}\n"
        )

    @staticmethod
    def _parse(raw: str) -> dict:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ReporterError(f"Reporter output was not valid JSON: {e}\n{raw[:500]}") from e

        if not isinstance(parsed, dict):
            raise ReporterError("Reporter output was not a JSON object")

        summary = parsed.get("executive_summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ReporterError("executive_summary must be a non-empty string")

        recommendation = parsed.get("recommendation")
        if recommendation not in _ALLOWED_RECOMMENDATIONS:
            raise ReporterError(
                f"recommendation must be one of {sorted(_ALLOWED_RECOMMENDATIONS)}, "
                f"got {recommendation!r}"
            )

        return {
            "executive_summary": summary.strip(),
            "recommendation": recommendation,
        }


def _tally(validation_results: list[dict]) -> dict[str, int]:
    healthy = degraded = failed = 0
    for v in validation_results:
        status = v.get("status")
        if status == "pass":
            healthy += 1
        elif status == "warn":
            degraded += 1
        elif status == "fail":
            failed += 1
    return {"healthy": healthy, "degraded": degraded, "failed": failed}


def _summarize_vm(v: dict) -> dict:
    return {
        "vm_id": v.get("vm_id"),
        "vm_name": v.get("vm_name", ""),
        "status": v.get("status"),
        "summary": v.get("summary", ""),
        "findings": v.get("findings", []) or [],
        "remediation": v.get("remediation", []) or [],
    }


def render_pdf(report: dict) -> bytes:
    """Render a WaveReport dict as a CISO-ready PDF and return the bytes."""
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title=f"VirtValidate Wave {report['wave_number']} Report",
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=8)
    heading = ParagraphStyle(
        "heading",
        parent=styles["Heading2"],
        fontSize=13,
        spaceBefore=12,
        spaceAfter=6,
    )

    story = []
    story.append(
        Paragraph(
            f"VirtValidate — Wave {report['wave_number']} Migration Report",
            styles["Title"],
        )
    )
    story.append(Spacer(1, 0.15 * inch))

    rec = report["recommendation"].upper()
    rec_color = {
        "PROCEED": colors.HexColor("#166534"),
        "HOLD": colors.HexColor("#92400e"),
        "ESCALATE": colors.HexColor("#991b1b"),
    }.get(rec, colors.black)
    story.append(
        Paragraph(
            f'<b>Recommendation:</b> <font color="{rec_color.hexval()}">{rec}</font>',
            body,
        )
    )

    totals = [
        ["Total VMs", "Healthy", "Degraded", "Failed"],
        [
            str(report["total_vms"]),
            str(report["healthy_count"]),
            str(report["degraded_count"]),
            str(report["failed_count"]),
        ],
    ]
    table = Table(totals, colWidths=[1.4 * inch] * 4)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#d1d5db")),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("TEXTCOLOR", (1, 1), (1, 1), colors.HexColor("#166534")),
                ("TEXTCOLOR", (2, 1), (2, 1), colors.HexColor("#92400e")),
                ("TEXTCOLOR", (3, 1), (3, 1), colors.HexColor("#991b1b")),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 0.2 * inch))

    story.append(Paragraph("Executive Summary", heading))
    story.append(Paragraph(_escape(report["executive_summary"]), body))

    story.append(Paragraph("Per-VM Findings", heading))
    for vm in report["per_vm_findings"]:
        name = vm.get("vm_name") or f"vm_id={vm.get('vm_id')}"
        status = (vm.get("status") or "").upper()
        status_color = {
            "PASS": "#166534",
            "WARN": "#92400e",
            "FAIL": "#991b1b",
        }.get(status, "#111827")
        story.append(
            Paragraph(
                f"<b>{_escape(name)}</b> — " f'<font color="{status_color}">{status}</font>',
                body,
            )
        )
        if vm.get("summary"):
            story.append(Paragraph(_escape(vm["summary"]), body))
        findings = vm.get("findings") or []
        if findings:
            items = "<br/>".join(
                f"• [{_escape(f.get('severity', ''))}] "
                f"{_escape(f.get('category', ''))}: "
                f"{_escape(f.get('message', ''))}"
                for f in findings
            )
            story.append(Paragraph(items, body))
        story.append(Spacer(1, 0.08 * inch))

    doc.build(story)
    return buf.getvalue()


def _escape(s: object) -> str:
    text = "" if s is None else str(s)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
