"""
Network Design Review — gap analysis between source VMware networking and
a proposed OpenShift Virtualization design.

Inputs:
  - ``source_summary`` — structured rollup of the source VMware portgroups
    derived from ``app.models.vm.VM.vsphere_networks``
  - ``customer_notes`` — operator-supplied plain-English description of
    the source environment (security zones, DMZ boundaries, special
    requirements RVTools doesn't capture)
  - ``proposed_yaml`` — the operator's proposed OCP-Virt design as raw
    YAML manifests (CUDN, NAD, NetworkPolicy, Multus config)

Output: structured JSON the API persists into ``NetworkDesignReview``
and ``NetworkFinding`` rows. The LLM is instructed to mark every finding
with a confidence level so reviewers can prioritize the high-confidence
items and triage the rest.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.vm import VM

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are VirtValidate, an expert network architect reviewing a proposed
OpenShift Virtualization (OCP-Virt) network design against a source VMware
environment. Your job is to identify gaps, mismatches, missing resources, and
positive confirmations between the source and the proposed design.

You will receive:
  1. A structured source summary (vSphere portgroups + the VMs attached to each)
  2. Customer-provided plain-English notes about the source networking
  3. The proposed OpenShift YAML manifests (CUDN / NAD / NetworkPolicy / Multus)

Produce findings in four categories. Always include a few from each category
when possible — operators want to see what works, not just what's broken.

  COVERAGE GAPS: VLANs / portgroups in the source not represented in the
  proposed design; segments mentioned in the notes but absent from the YAML.

  CONFIGURATION MISMATCHES: MTU differences, VLAN tagging differences,
  IP address space conflicts, security zone separation lost in translation.

  MISSING RESOURCES: Customer mentions DMZ but no DMZ NetworkPolicy; source
  has dedicated backup network with no equivalent CUDN; multi-NIC source
  VMs but the proposed design only allows a single NAD.

  POSITIVE CONFIRMATIONS: Things the design correctly handles. Helps the
  operator know what they got right; pure-negative reports erode trust.

Each finding MUST include:
  - severity assessment with reasoning embedded in the description
  - source_evidence — exact quote or reference from source data / notes
  - proposed_evidence — exact quote or reference from the YAML, or
    explicit "(absent)" if nothing in the proposed design relates
  - recommendation — specific, actionable next step
  - confidence — your honest uncertainty: "high" when evidence is clear,
    "medium" when there's reasonable inference, "low" when the inputs
    are too thin to be sure

You are decision support, not authoritative validation. Mark "low" confidence
liberally when the customer notes are vague — that is the operator's signal
to talk to a human network engineer.

Respond with a SINGLE JSON object and nothing else. No markdown fences. Schema:
{
  "executive_summary": "one or two paragraphs summarizing the overall design fit",
  "findings": [
    {
      "category": "coverage_gap" | "config_mismatch" | "missing_resource" | "positive_confirmation",
      "severity": "critical" | "high" | "medium" | "low" | "info",
      "title": "<short headline, <100 chars>",
      "description": "<plain-English explanation, multiple sentences ok>",
      "source_evidence": "<quote or reference from source>",
      "proposed_evidence": "<quote or reference from proposed design, or '(absent)'>",
      "recommendation": "<actionable next step>",
      "confidence": "high" | "medium" | "low"
    }
  ]
}
"""


# ---------------------------------------------------------------------------
# Source summary builder — pulls from the VM table since RVTools state
# already lands there during enrollment.
# ---------------------------------------------------------------------------
def build_source_summary(db: Session) -> dict:
    """Aggregate vSphere networks across all enrolled VMs.

    Returns a dict the prompt renderer can dump as JSON. Each portgroup
    carries the count of VMs attached + a sample of names so the LLM has
    enough context to reason about its purpose.
    """
    vms = list(db.scalars(select(VM)).all())
    network_counts: Counter[str] = Counter()
    network_samples: dict[str, list[str]] = {}
    datastore_counts: Counter[str] = Counter()
    target_namespaces: Counter[str] = Counter()
    target_storage_classes: Counter[str] = Counter()
    target_nads: Counter[str] = Counter()

    for vm in vms:
        for n in vm.vsphere_networks or []:
            network_counts[n] += 1
            samples = network_samples.setdefault(n, [])
            if len(samples) < 5:
                samples.append(vm.name)
        for d in vm.vsphere_datastores or []:
            datastore_counts[d] += 1
        if vm.target_namespace:
            target_namespaces[vm.target_namespace] += 1
        if vm.target_storage_class:
            target_storage_classes[vm.target_storage_class] += 1
        if vm.target_network_attachment:
            target_nads[vm.target_network_attachment] += 1

    return {
        "total_vms": len(vms),
        "vsphere_networks": [
            {"name": name, "vm_count": count, "sample_vms": network_samples[name]}
            for name, count in sorted(network_counts.items(), key=lambda x: -x[1])
        ],
        "vsphere_datastores": [
            {"name": name, "vm_count": count}
            for name, count in sorted(datastore_counts.items(), key=lambda x: -x[1])
        ],
        # Operator-stated target intent — useful context for the LLM when
        # the proposed YAML doesn't include the same names.
        "target_namespaces_declared": dict(target_namespaces),
        "target_storage_classes_declared": dict(target_storage_classes),
        "target_network_attachments_declared": dict(target_nads),
    }


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
class NetworkReviewError(RuntimeError):
    """Raised when the LLM call fails or returns unparseable output."""


_ALLOWED_CATEGORY = {
    "coverage_gap",
    "config_mismatch",
    "missing_resource",
    "positive_confirmation",
}
_ALLOWED_SEVERITY = {"critical", "high", "medium", "low", "info"}
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}


class NetworkReviewer:
    """Wraps the local Ollama call. Same pattern as MigrationPlanner /
    LLMClient — a single ``analyze()`` method, JSON-format request, strict
    response validation that surfaces LLM drift as ``NetworkReviewError``."""

    def __init__(
        self,
        host: str | None = None,
        model: str | None = None,
        timeout: float = 240.0,
    ) -> None:
        self.host = (host or settings.ollama_host).rstrip("/")
        self.model = model or settings.ollama_model
        self.timeout = timeout

    def analyze(
        self,
        *,
        source_summary: dict,
        customer_notes: str,
        proposed_yaml: str,
    ) -> dict:
        prompt = self._render_prompt(source_summary, customer_notes, proposed_yaml)
        raw = self._chat(SYSTEM_PROMPT, prompt)
        return self._parse(raw)

    def _chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.15},
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
            raise NetworkReviewError(f"Ollama request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise NetworkReviewError(f"Ollama returned non-JSON envelope: {e}") from e

        content = (body.get("message") or {}).get("content", "")
        if not content:
            raise NetworkReviewError("Ollama returned an empty message")
        return content

    @staticmethod
    def _render_prompt(source_summary: dict, customer_notes: str, proposed_yaml: str) -> str:
        notes_block = customer_notes.strip() or "(no notes provided — analysis will be limited)"
        yaml_block = (
            proposed_yaml.strip() or "(no proposed YAML provided — analysis will be limited)"
        )
        return (
            "## Source environment summary (from RVTools / baseline collection)\n\n"
            f"```json\n{json.dumps(source_summary, indent=2, sort_keys=True, default=str)}\n```\n\n"
            "## Customer notes (plain English)\n\n"
            f"{notes_block}\n\n"
            "## Proposed OpenShift YAML manifests\n\n"
            f"```yaml\n{yaml_block}\n```\n\n"
            "Produce the JSON object specified in the system prompt now."
        )

    @staticmethod
    def _parse(raw: str) -> dict:
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            raise NetworkReviewError(
                f"Network review output was not valid JSON: {e}\n{raw[:500]}"
            ) from e

        if not isinstance(payload, dict):
            raise NetworkReviewError("Network review output was not a JSON object")

        summary = payload.get("executive_summary", "")
        if not isinstance(summary, str):
            summary = ""

        findings_raw = payload.get("findings")
        if not isinstance(findings_raw, list):
            raise NetworkReviewError("Network review output missing 'findings' list")

        findings: list[dict] = []
        for idx, f in enumerate(findings_raw):
            if not isinstance(f, dict):
                raise NetworkReviewError(f"Finding at index {idx} is not an object")
            findings.append(_normalize_finding(idx, f))

        return {"executive_summary": summary, "findings": findings}


def _normalize_finding(idx: int, raw: dict) -> dict:
    """Validate + normalize a single finding from the LLM output.

    Loose-but-strict: enforce the enums but tolerate missing optional
    string fields (description / source_evidence / etc.) since LLMs
    occasionally drop them and re-prompting gets expensive.
    """
    category = raw.get("category")
    if category not in _ALLOWED_CATEGORY:
        raise NetworkReviewError(
            f"Finding {idx}: category must be one of {sorted(_ALLOWED_CATEGORY)}, got {category!r}"
        )
    severity = raw.get("severity")
    if severity not in _ALLOWED_SEVERITY:
        raise NetworkReviewError(
            f"Finding {idx}: severity must be one of {sorted(_ALLOWED_SEVERITY)}, got {severity!r}"
        )
    confidence = raw.get("confidence", "medium")
    if confidence not in _ALLOWED_CONFIDENCE:
        # Don't fail the whole review for a single confidence string — the
        # reviewer can still triage. Default to medium and move on.
        confidence = "medium"

    title = (raw.get("title") or "").strip() or "(untitled finding)"
    return {
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "title": title[:255],
        "description": (raw.get("description") or "").strip(),
        "source_evidence": (raw.get("source_evidence") or "").strip(),
        "proposed_evidence": (raw.get("proposed_evidence") or "").strip(),
        "recommendation": (raw.get("recommendation") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Helpers used by the API + background task
# ---------------------------------------------------------------------------
def normalize_findings(items: Iterable[dict]) -> list[dict]:
    """Public re-export for callers that already have raw finding dicts
    (e.g., a future "import findings from JSON" feature) and want the
    same validation pass the analyzer applies."""
    return [_normalize_finding(i, x) for i, x in enumerate(items)]
