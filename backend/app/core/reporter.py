"""
Wave report generator.

Aggregates per-VM validation verdicts for a single migration wave and asks the
local Ollama model for a CISO-level executive summary, then returns a structured
WaveReport plus a PDF rendering (weasyprint).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from html import escape as _html_escape
from typing import Literal

import httpx

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


def _e(s: object) -> str:
    """HTML-escape an arbitrary value, treating None as empty string."""
    return _html_escape("" if s is None else str(s), quote=True)


_PDF_CSS = """
/* xhtml2pdf does NOT support @page margin-boxes (@bottom-left etc.) nor
   CSS string-set + string(). Page-number footer lives in an @frame
   footer rendered from #footer_content in the body instead. Flex layout
   isn't supported either, so .header and .vm-header use table-cell
   layout. clip-path isn't supported, so .logo is a plain square. */
@page {
  size: Letter;
  margin: 1.6cm 1.8cm 2.4cm 1.8cm;
  @frame footer_frame {
    -pdf-frame-content: footer_content;
    left: 1.8cm; bottom: 1.2cm; height: 0.6cm; width: 17.4cm;
  }
}
body {
  font-family: "Helvetica", "Arial", sans-serif;
  color: #111827;
  font-size: 10pt;
  line-height: 1.5;
}
#footer_content {
  font-size: 8pt;
  color: #6b7280;
  font-family: "Helvetica", "Arial", sans-serif;
}
#footer_content .right { text-align: right; }

.header {
  width: 100%;
  border-bottom: 3px solid #1d4ed8;
  padding-bottom: 12px;
  margin-bottom: 18px;
}
.header-table { width: 100%; border-collapse: collapse; }
.header-table td { vertical-align: middle; }
.logo-cell { width: 50px; padding-right: 14px; }
.logo {
  width: 36px; height: 36px;
  background: #1d4ed8;
  color: white;
  font-weight: 700;
  font-size: 18pt;
  text-align: center;
  line-height: 36px;
}
.brand-block .brand {
  font-size: 17pt; font-weight: 700; color: #1d4ed8;
  letter-spacing: 0.06em;
}
.brand-block .subtitle {
  font-size: 8pt; color: #6b7280; letter-spacing: 0.18em;
  text-transform: uppercase;
}

h1 { font-size: 16pt; margin: 0 0 4px; color: #111827; }
h2 {
  font-size: 12pt; margin: 18px 0 8px;
  padding-bottom: 4px; border-bottom: 1px solid #e5e7eb;
  color: #1f2937;
}
h3 {
  font-size: 11pt; margin: 14px 0 4px;
  color: #1f2937;
}

.recommendation {
  font-size: 11pt;
  font-weight: 700;
  padding: 10px 14px;
  margin: 10px 0 14px;
  border-radius: 3px;
  letter-spacing: 0.05em;
}
.recommendation.proceed  { background: #dcfce7; color: #166534; border-left: 4px solid #166534; }
.recommendation.hold     { background: #fef3c7; color: #92400e; border-left: 4px solid #92400e; }
.recommendation.escalate { background: #fee2e2; color: #991b1b; border-left: 4px solid #991b1b; }

.stats {
  display: table;
  width: 100%;
  border-collapse: separate;
  border-spacing: 6px 0;
  margin: 6px 0 14px;
}
.stat {
  display: table-cell;
  border: 1px solid #e5e7eb;
  border-radius: 3px;
  padding: 10px 12px;
  text-align: center;
  width: 25%;
}
.stat .label {
  font-size: 8pt; color: #6b7280;
  letter-spacing: 0.12em; text-transform: uppercase;
}
.stat .value {
  font-size: 18pt; font-weight: 700;
  margin-top: 4px;
  font-family: "Helvetica", "Arial", sans-serif;
}
.stat.total    .value { color: #111827; }
.stat.healthy  .value { color: #166534; }
.stat.degraded .value { color: #92400e; }
.stat.failed   .value { color: #991b1b; }

.exec-summary {
  background: #f9fafb;
  padding: 12px 14px;
  border-left: 3px solid #1d4ed8;
  font-size: 10pt;
  color: #1f2937;
}

.vm {
  page-break-inside: avoid;
  margin: 14px 0 8px;
  border: 1px solid #e5e7eb;
  border-radius: 3px;
  padding: 12px 14px;
}
.vm-header { width: 100%; border-collapse: collapse; }
.vm-header td { vertical-align: middle; }
.vm-header td.status-cell { text-align: right; width: 80px; }
.vm-name { font-size: 11pt; font-weight: 700; color: #111827; }
.vm-status {
  display: inline-block;
  font-size: 8pt;
  padding: 2px 8px;
  border-radius: 2px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.vm-status.pass { background: #dcfce7; color: #166534; }
.vm-status.warn { background: #fef3c7; color: #92400e; }
.vm-status.fail { background: #fee2e2; color: #991b1b; }

.vm-summary {
  margin: 6px 0 8px;
  font-size: 9.5pt;
  color: #374151;
}

table.findings {
  width: 100%;
  border-collapse: collapse;
  font-size: 9pt;
  margin-top: 6px;
}
table.findings th {
  background: #1f2937;
  color: white;
  text-align: left;
  padding: 6px 8px;
  font-weight: 600;
  letter-spacing: 0.04em;
}
table.findings td {
  padding: 6px 8px;
  border-bottom: 1px solid #e5e7eb;
  vertical-align: top;
}
table.findings col.sev  { width: 14%; }
table.findings col.find { width: 43%; }
table.findings col.rem  { width: 43%; }
.severity {
  display: inline-block;
  font-size: 7.5pt;
  padding: 1px 6px;
  border-radius: 2px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
.severity.critical { background: #991b1b; color: white; }
.severity.warn     { background: #f59e0b; color: white; }
.severity.info     { background: #3b82f6; color: white; }
.severity.none     { background: #e5e7eb; color: #6b7280; }
code {
  background: #f3f4f6;
  padding: 1px 4px;
  border-radius: 2px;
  font-size: 8.5pt;
  font-family: "Courier New", monospace;
  color: #1f2937;
}
.empty { color: #9ca3af; font-style: italic; font-size: 9pt; }
"""


def _severity_class(sev: str | None) -> str:
    s = (sev or "").lower()
    if s in {"critical", "warn", "info"}:
        return s
    return "none"


def _format_remediation_cell(r: dict | None) -> str:
    if not r:
        return '<span class="empty">—</span>'
    action = _e(r.get("action", ""))
    cmd = r.get("command")
    if cmd:
        return f"{action}<br/><code>{_e(cmd)}</code>"
    return action or '<span class="empty">—</span>'


def _build_vm_block(vm: dict) -> str:
    name = vm.get("vm_name") or f"vm_id={vm.get('vm_id')}"
    status = (vm.get("status") or "").lower()
    status_label = status.upper() or "—"
    summary = vm.get("summary", "")

    findings = list(vm.get("findings") or [])
    remediation = list(vm.get("remediation") or [])

    if not findings and not remediation:
        rows = '<tr><td colspan="3" class="empty">No findings recorded.</td></tr>'
    else:
        # Pair finding[i] with remediation[i]; if either list is longer, pad
        # the shorter side with empty cells so every step still renders.
        rows_count = max(len(findings), len(remediation))
        out = []
        for i in range(rows_count):
            f = findings[i] if i < len(findings) else None
            r = remediation[i] if i < len(remediation) else None
            sev = (f or {}).get("severity") or ""
            sev_class = _severity_class(sev)
            sev_cell = f'<span class="severity {sev_class}">{_e(sev) or "—"}</span>'
            category = _e((f or {}).get("category", ""))
            message = _e((f or {}).get("message", ""))
            if f:
                find_cell = f"<strong>{category}</strong><br/>{message}" if category else message
            else:
                find_cell = '<span class="empty">—</span>'
            rem_cell = _format_remediation_cell(r)
            out.append(f"<tr><td>{sev_cell}</td><td>{find_cell}</td><td>{rem_cell}</td></tr>")
        rows = "\n".join(out)

    summary_html = f'<div class="vm-summary">{_e(summary)}</div>' if summary else ""
    return f"""<div class="vm">
  <table class="vm-header"><tr>
    <td><span class="vm-name">{_e(name)}</span></td>
    <td class="status-cell"><span class="vm-status {status}">{_e(status_label)}</span></td>
  </tr></table>
  {summary_html}
  <table class="findings">
    <colgroup><col class="sev"/><col class="find"/><col class="rem"/></colgroup>
    <thead><tr><th>Severity</th><th>Finding</th><th>Remediation</th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</div>"""


def _build_pdf_html(
    report: dict,
    *,
    generated_at: datetime,
    cluster_name: str,
) -> str:
    """Build the standalone HTML document weasyprint renders into a PDF."""
    rec = (report.get("recommendation") or "").lower()
    rec_class = rec if rec in {"proceed", "hold", "escalate"} else "hold"
    timestamp = generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")

    vm_blocks = (
        "\n".join(_build_vm_block(vm) for vm in report.get("per_vm_findings", []))
        or '<div class="empty">No VMs in this wave.</div>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>VirtValidate — Wave {_e(report.get('wave_number'))} Report</title>
<style>{_PDF_CSS}</style>
</head>
<body>

<div class="header">
  <table class="header-table"><tr>
    <td class="logo-cell"><div class="logo">V</div></td>
    <td>
      <div class="brand-block">
        <div class="brand">VIRTVALIDATE</div>
        <div class="subtitle">VM Migration Validation Platform</div>
      </div>
    </td>
  </tr></table>
</div>

<h1>Wave {_e(report.get('wave_number'))} — Migration Report</h1>

<div class="recommendation {rec_class}">
  Recommendation: {_e(rec.upper() or "—")}
</div>

<div class="stats">
  <div class="stat total"><div class="label">Total VMs</div><div class="value">{_e(report.get('total_vms', 0))}</div></div>
  <div class="stat healthy"><div class="label">Healthy</div><div class="value">{_e(report.get('healthy_count', 0))}</div></div>
  <div class="stat degraded"><div class="label">Degraded</div><div class="value">{_e(report.get('degraded_count', 0))}</div></div>
  <div class="stat failed"><div class="label">Failed</div><div class="value">{_e(report.get('failed_count', 0))}</div></div>
</div>

<h2>Executive Summary</h2>
<div class="exec-summary">{_e(report.get('executive_summary', ''))}</div>

<h2>Per-VM Findings</h2>
{vm_blocks}

<div id="footer_content">
  <table style="width:100%;"><tr>
    <td>Generated {_e(timestamp)} · cluster {_e(cluster_name)}</td>
    <td class="right">Page <pdf:pagenumber/> of <pdf:pagecount/></td>
  </tr></table>
</div>
</body>
</html>"""


def render_pdf(
    report: dict,
    *,
    generated_at: datetime | None = None,
    cluster_name: str | None = None,
) -> bytes:
    """Render a WaveReport dict as a CISO-ready PDF.

    Uses xhtml2pdf (pure Python, ReportLab under the hood) instead of
    weasyprint so the same code runs identically on both the standard
    UBI image and the hardened Project Hummingbird image — the latter
    doesn't ship libcairo / libpango / libgdk-pixbuf. See PR #12 +
    docs/SECURITY_POSTURE.md for the dual-variant rationale.
    """
    # Lazy import so the rest of the reporter module can be exercised in
    # tests without the renderer dep installed.
    from io import BytesIO  # noqa: PLC0415
    from xhtml2pdf import pisa  # noqa: PLC0415

    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    if cluster_name is None:
        cluster_name = settings.cluster_name

    html = _build_pdf_html(report, generated_at=generated_at, cluster_name=cluster_name)
    buf = BytesIO()
    result = pisa.CreatePDF(html, dest=buf, encoding="utf-8")
    if result.err:
        # xhtml2pdf's "err" counts logged errors but doesn't always raise.
        # Treat any reported error as a hard failure so callers see a 5xx
        # rather than a silently-malformed PDF.
        raise RuntimeError(f"PDF rendering failed: {result.err} error(s)")
    return buf.getvalue()
