"""Storage Design Review — gap analysis between source VMware datastore
layout and a proposed OpenShift Virtualization storage design.

Sister analyzer to ``app.core.network_review``. The shape of the LLM
call + response is identical; what differs is the prompt focus and
the source-summary builder. Storage findings emphasize:

  - **Performance tier mismatches** — SSD source → HDD target, etc.
  - **Capacity concerns** — over-provisioning risks, thin-provisioned
    VMs landing on a StorageClass that doesn't support thin.
  - **Access mode mismatches** — multi-attach (RWX) intent on VMware
    landing on a ReadWriteOnce StorageClass.
  - **Replication / HA loss** — replicated arrays without an
    equivalent in the proposed design.
  - **Snapshot capability** — VMware snapshots without a
    VolumeSnapshotClass on the OCP side.
  - **Multipath policy** — ALUA / round-robin choices preserved.

Honest limitation: the appliance only sees datastore **names** in
``VM.vsphere_datastores``. Tier metadata (SSD vs HDD), capacity
utilization, and replication topology come from the **operator's
notes** — the LLM is explicit about flagging "low confidence" when
notes don't carry the relevant detail.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.runtime import get_active_backend
from app.models.vm import VM

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are VirtValidate, an expert storage architect reviewing a proposed
OpenShift Virtualization (OCP-Virt) storage design against a source VMware
environment. Your job is to identify gaps, mismatches, missing resources, and
positive confirmations between the source datastore layout and the proposed
StorageClass / VolumeSnapshotClass / StorageMap design.

You will receive:
  1. A structured source summary (vSphere datastores + the VMs attached to each,
     plus declared target namespaces / StorageClasses)
  2. Customer-provided plain-English notes about storage tiers, performance
     requirements, backup/replication topology, multipath policies
  3. The proposed OpenShift YAML manifests (StorageClass, VolumeSnapshotClass,
     StorageMap, optional StorageProfile)

Produce findings in seven categories. Always include positive confirmations
when applicable — pure-negative reports erode trust:

  PERFORMANCE TIER MISMATCH: VM was on SSD-backed datastore but mapped to a
    StorageClass backed by HDD or general-purpose storage. Customer notes
    mention "production databases need <5ms latency" but the proposed
    StorageClass is shared general-purpose. High-IOPS workload mapped to
    throughput-optimized storage.

  CAPACITY CONCERN: Source datastore at 80%+ utilization being consolidated
    to smaller proposed storage. VMs with thin-provisioned disks landing on
    a StorageClass that doesn't support thin provisioning (silent
    over-provisioning risk). Aggregate VM disk capacity exceeds proposed
    StorageClass quota.

  ACCESS MODE MISMATCH: VM with shared disk on VMware (RWX intent — Oracle
    RAC, Microsoft cluster, GFS2) mapped to ReadWriteOnce StorageClass.
    Multi-attach scenarios not represented in proposed design.

  REPLICATION LOSS: Customer notes mention "datastore is on replicated array"
    but proposed StorageClass has no replication. VM has multiple disks
    spread across distinct datastores for HA / failure-domain isolation but
    mapped to single StorageClass that collapses that isolation.

  SNAPSHOT CAPABILITY: VM relies on VMware snapshots for backup; proposed
    StorageClass doesn't support CSI snapshots. VolumeSnapshotClass missing
    for the StorageClass referenced. CSI driver in use doesn't implement
    snapshots.

  MULTIPATH POLICY: Source VM has specific multipath policy (ALUA failover,
    round-robin, fixed path). Proposed storage doesn't specify or supports
    a different model.

  POSITIVE CONFIRMATION: Performance tier alignment confirmed. Capacity
    headroom adequate. Replication preserved by the proposed design.
    Snapshot capability maintained via CSI + VolumeSnapshotClass.

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
liberally when the customer notes are vague — that's the operator's signal
to talk to a human storage engineer.

KEY LIMITATIONS (mention in your reasoning when relevant, do not silently
ignore):
  - You cannot validate actual storage performance — only declared specs.
  - StorageClass capabilities depend on the backing CSI driver's behavior.
  - Snapshot quiescence requires application coordination not validated here.
  - Datastore tier (SSD vs HDD) is only known if the customer notes carry it.
    The appliance sees datastore names only.

Respond with a SINGLE JSON object and nothing else. No markdown fences. Schema:
{
  "executive_summary": "one or two paragraphs summarizing the overall design fit",
  "findings": [
    {
      "category": "performance_tier_mismatch" | "capacity_concern" |
                  "access_mode_mismatch" | "replication_loss" |
                  "snapshot_capability" | "multipath_policy" |
                  "positive_confirmation",
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
# Source summary builder
# ---------------------------------------------------------------------------
def build_storage_source_summary(db: Session) -> dict:
    """Aggregate vSphere datastores + per-datastore VM membership.

    The summary feeds the prompt's "source environment" block. Each
    datastore carries the count of attached VMs + a sample of names
    so the LLM has enough context to reason about purpose ("the
    'nfs-prod-fast' datastore has 200 VMs whose names start with
    'db-' — likely the production database tier").

    We deliberately keep this minimal — the appliance only sees
    datastore *names*, not tiers / capacity / replication topology.
    Those signals come from the customer notes.
    """
    vms = list(db.scalars(select(VM)).all())

    datastore_counts: Counter[str] = Counter()
    datastore_samples: dict[str, list[str]] = {}
    multi_datastore_vms = 0
    target_storage_classes: Counter[str] = Counter()
    target_namespaces: Counter[str] = Counter()

    for vm in vms:
        ds_list = vm.vsphere_datastores or []
        if len(ds_list) > 1:
            # VM with disks across multiple datastores — likely an HA
            # spread the operator wants preserved. Surface the count
            # so the LLM can flag it.
            multi_datastore_vms += 1
        for d in ds_list:
            datastore_counts[d] += 1
            samples = datastore_samples.setdefault(d, [])
            if len(samples) < 5:
                samples.append(vm.name)
        # Per-VM target_storage_class was dropped in the multi-cluster
        # target architecture migration; storage routing now comes
        # exclusively from the ResourceMapping for the VM's
        # (vcenter, cluster) pair. Aggregating per-VM here is no longer
        # meaningful.
        if vm.target_namespace_override:
            target_namespaces[vm.target_namespace_override] += 1

    return {
        "total_vms": len(vms),
        "vsphere_datastores": [
            {
                "name": name,
                "vm_count": count,
                "sample_vms": datastore_samples[name],
            }
            for name, count in sorted(datastore_counts.items(), key=lambda x: -x[1])
        ],
        # VMs with disks across multiple datastores — strong signal
        # the operator wants HA / failure-domain spread preserved.
        "vms_spanning_multiple_datastores": multi_datastore_vms,
        # Operator-stated target intent — useful context when the
        # proposed YAML doesn't reference the same StorageClass names.
        "target_storage_classes_declared": dict(target_storage_classes),
        "target_namespaces_declared": dict(target_namespaces),
    }


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------
class StorageReviewError(RuntimeError):
    """Raised when the LLM call fails or returns unparseable output."""


_ALLOWED_CATEGORY = {
    "performance_tier_mismatch",
    "capacity_concern",
    "access_mode_mismatch",
    "replication_loss",
    "snapshot_capability",
    "multipath_policy",
    "positive_confirmation",
}
_ALLOWED_SEVERITY = {"critical", "high", "medium", "low", "info"}
_ALLOWED_CONFIDENCE = {"high", "medium", "low"}


class StorageReviewer:
    """Wraps the LLM call. Same DI pattern as NetworkReviewer / LLMClient
    — takes an ``LLMBackend`` in its constructor; defaults to the
    factory. Single ``analyze()`` entry point."""

    def __init__(
        self,
        backend: LLMBackend | None = None,
    ) -> None:
        self.backend = backend or get_active_backend()

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
        try:
            response = self.backend.chat_sync(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.15,
            )
        except LLMBackendError as e:
            raise StorageReviewError(str(e)) from e
        content = response.get("content", "")
        if not content:
            raise StorageReviewError("LLM backend returned an empty message")
        return content

    @staticmethod
    def _render_prompt(source_summary: dict, customer_notes: str, proposed_yaml: str) -> str:
        notes_block = customer_notes.strip() or (
            "(no notes provided — analysis will be limited; storage tier, "
            "replication, and snapshot requirements need explicit input)"
        )
        yaml_block = (
            proposed_yaml.strip() or "(no proposed YAML provided — analysis will be limited)"
        )
        return (
            "## Source environment summary (from RVTools / baseline collection)\n\n"
            f"```json\n{json.dumps(source_summary, indent=2, sort_keys=True, default=str)}\n```\n\n"
            "## Customer notes (plain English)\n\n"
            f"{notes_block}\n\n"
            "## Proposed OpenShift storage YAML manifests\n\n"
            f"```yaml\n{yaml_block}\n```\n\n"
            "Produce the JSON object specified in the system prompt now."
        )

    @staticmethod
    def _parse(raw: str) -> dict:
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError as e:
            raise StorageReviewError(
                f"Storage review output was not valid JSON: {e}\n{raw[:500]}"
            ) from e

        if not isinstance(payload, dict):
            raise StorageReviewError("Storage review output was not a JSON object")

        summary = payload.get("executive_summary", "")
        if not isinstance(summary, str):
            summary = ""

        findings_raw = payload.get("findings")
        if not isinstance(findings_raw, list):
            raise StorageReviewError("Storage review output missing 'findings' list")

        findings: list[dict] = []
        for idx, f in enumerate(findings_raw):
            if not isinstance(f, dict):
                raise StorageReviewError(f"Finding at index {idx} is not an object")
            findings.append(_normalize_finding(idx, f))

        return {"executive_summary": summary, "findings": findings}


def _normalize_finding(idx: int, raw: dict) -> dict:
    """Validate + normalize a single finding from the LLM output.

    Loose-but-strict — same approach as the network reviewer.
    Enforce enums on category + severity (those drive UI rendering)
    but tolerate missing optional string fields, since LLMs
    occasionally drop them and re-prompting gets expensive.
    """
    category = raw.get("category")
    if category not in _ALLOWED_CATEGORY:
        raise StorageReviewError(
            f"Finding {idx}: category must be one of {sorted(_ALLOWED_CATEGORY)}, "
            f"got {category!r}"
        )
    severity = raw.get("severity")
    if severity not in _ALLOWED_SEVERITY:
        raise StorageReviewError(
            f"Finding {idx}: severity must be one of {sorted(_ALLOWED_SEVERITY)}, "
            f"got {severity!r}"
        )
    confidence = raw.get("confidence", "medium")
    if confidence not in _ALLOWED_CONFIDENCE:
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


def normalize_findings(items: Iterable[dict]) -> list[dict]:
    """Public re-export — same shape as network_review.normalize_findings.
    Used when an external caller (e.g., a future "import findings from
    JSON" feature) wants the same validation pass the analyzer applies."""
    return [_normalize_finding(i, x) for i, x in enumerate(items)]
