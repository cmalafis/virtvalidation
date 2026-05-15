"""LLM-driven suggestion engine for resource mappings.

Given a vCenter source (with its discovered networks and datastores)
and an OCP target (with its discovered StorageClasses and network
attachments), the LLM proposes a mapping between them with a
confidence score per row.

Operators see the suggestions in the mapping editor and accept,
adjust, or replace each one. The LLM never auto-commits — every
row is reviewed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.runtime import get_active_backend

logger = logging.getLogger(__name__)


NETWORK_SYSTEM_PROMPT = """You are VirtValidate, an expert OpenShift Virtualization architect
proposing network mappings between a source VMware environment and a
target OpenShift cluster. The customer is migrating VMs and needs each
source vSphere portgroup mapped to a target OCP network resource
(NetworkAttachmentDefinition, ClusterUserDefinedNetwork, or
UserDefinedNetwork).

You will receive:
  - a list of source vSphere networks (names + how many VMs are on each)
  - a list of target OCP network resources (name, type, namespace)

For each source network, propose ONE target network. Match by:
  1. Name correspondence (e.g. "DMZ-Web-VLAN-100" → target containing "dmz" or "web")
  2. Type correspondence (production segments → CUDN, isolated apps → NAD)
  3. Tier / zone semantics (DMZ → DMZ, prod → prod)

If no clean match exists, propose your best guess with confidence "low" and
explain in rationale. NEVER invent target network names that aren't in the
provided list.

Confidence levels:
  - "high": exact or near-exact name match + same type
  - "medium": semantic match by tier/zone but name differs
  - "low": weak signal, operator should review carefully

Respond with a SINGLE JSON object:
{
  "rationale_summary": "one or two sentences about the overall matching approach",
  "suggestions": [
    {
      "source_network": "<verbatim source name>",
      "target_network_name": "<verbatim target name from the list>",
      "target_network_type": "nad" | "cudn" | "udn" | "pod",
      "target_namespace": "<target namespace from the list, or null for cluster-scoped CUDN>",
      "confidence": "high" | "medium" | "low",
      "rationale": "one short clause"
    }
  ]
}
No markdown fences. Every source_network from the input MUST appear in
the suggestions list exactly once.
"""


STORAGE_SYSTEM_PROMPT = """You are VirtValidate, proposing StorageClass mappings between source
vSphere datastores and target OpenShift StorageClasses. The customer is
migrating VMs and needs each source datastore mapped.

You will receive:
  - a list of source datastores (names + VM counts + any tier hints from
    customer notes if present)
  - a list of target StorageClasses (name, provisioner, default flag,
    reclaim policy)

For each source datastore, propose ONE target StorageClass. Match by:
  1. Name correspondence and tier hints (SSD source → SSD-named target,
     HDD/bulk source → bulk-named target)
  2. Provisioner type alignment when names alone are ambiguous
     (Ceph RBD generally aligns to block storage; CephFS to RWX use cases)
  3. Default StorageClass for ambiguous cases

If a source datastore is for shared-disk patterns (Oracle RAC, multi-attach),
prefer a StorageClass whose provisioner supports ReadWriteMany (CephFS,
NFS-CSI). The ``access_modes`` hint on each target reflects what the
provisioner commonly supports — use it as a tiebreaker.

NEVER invent StorageClass names that aren't in the provided list.

Confidence levels:
  - "high": clear tier match (SSD → SSD)
  - "medium": same provisioner family but different tier
  - "low": guessing; operator should review

Respond with a SINGLE JSON object:
{
  "rationale_summary": "one or two sentences about the overall matching approach",
  "suggestions": [
    {
      "source_datastore": "<verbatim source name>",
      "target_storage_class": "<verbatim target name from the list>",
      "access_mode": "ReadWriteOnce" | "ReadWriteMany" | "ReadOnlyMany",
      "confidence": "high" | "medium" | "low",
      "rationale": "one short clause"
    }
  ]
}
No markdown fences. Every source_datastore from the input MUST appear in
the suggestions list exactly once.
"""


class SuggestionError(RuntimeError):
    """Raised when the LLM call fails or produces unparseable output."""


def suggest_network_mappings(
    *,
    sources: list[dict],
    targets: list[dict],
    backend: LLMBackend | None = None,
) -> dict:
    """Ask the LLM for a network-mapping proposal.

    ``sources`` shape: ``[{"name": str, "vm_count": int}, ...]``
    ``targets`` shape: ``[{"name": str, "type": str, "namespace": str?}, ...]``
    """
    if not sources:
        return {"suggestions": [], "rationale_summary": "No source networks to map."}
    if not targets:
        return {
            "suggestions": [],
            "rationale_summary": (
                "No target networks discovered on the cluster — run discovery first."
            ),
        }
    backend = backend or get_active_backend()
    user_msg = (
        "## Source vSphere networks\n\n"
        f"```json\n{json.dumps(sources, indent=2)}\n```\n\n"
        "## Target OCP network resources\n\n"
        f"```json\n{json.dumps(targets, indent=2)}\n```\n\n"
        "Produce the JSON object specified in the system prompt now."
    )
    return _call_and_parse(
        backend,
        NETWORK_SYSTEM_PROMPT,
        user_msg,
        key="source_network",
        target_set={t["name"] for t in targets},
    )


def suggest_storage_mappings(
    *,
    sources: list[dict],
    targets: list[dict],
    backend: LLMBackend | None = None,
) -> dict:
    """Ask the LLM for a storage-mapping proposal.

    ``sources`` shape: ``[{"name": str, "vm_count": int, "tier_hint": str?}, ...]``
    ``targets`` shape: ``[{"name": str, "provisioner": str, "is_default": bool,
                           "access_modes": list[str]}, ...]``
    """
    if not sources:
        return {"suggestions": [], "rationale_summary": "No source datastores to map."}
    if not targets:
        return {
            "suggestions": [],
            "rationale_summary": (
                "No target StorageClasses discovered on the cluster — run discovery first."
            ),
        }
    backend = backend or get_active_backend()
    user_msg = (
        "## Source vSphere datastores\n\n"
        f"```json\n{json.dumps(sources, indent=2)}\n```\n\n"
        "## Target OCP StorageClasses\n\n"
        f"```json\n{json.dumps(targets, indent=2)}\n```\n\n"
        "Produce the JSON object specified in the system prompt now."
    )
    return _call_and_parse(
        backend,
        STORAGE_SYSTEM_PROMPT,
        user_msg,
        key="source_datastore",
        target_set={t["name"] for t in targets},
    )


def _call_and_parse(
    backend: LLMBackend,
    system_prompt: str,
    user_msg: str,
    *,
    key: str,
    target_set: set[str],
) -> dict:
    """Wrap the LLM call + parse + validate. The validation rule the
    parser enforces: every suggestion's referenced target name must
    actually exist on the cluster. Hallucinated targets get dropped
    and noted in the rationale_summary so operators see the gap."""
    try:
        response = backend.chat_sync(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )
    except LLMBackendError as e:
        raise SuggestionError(f"LLM backend failure: {e}") from e
    raw = response.get("content", "")
    if not raw:
        raise SuggestionError("LLM returned empty content")
    try:
        body: Any = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SuggestionError(f"LLM output not valid JSON: {e}") from e
    if not isinstance(body, dict):
        raise SuggestionError("LLM output not a JSON object")
    suggestions = body.get("suggestions") or []
    if not isinstance(suggestions, list):
        raise SuggestionError("LLM output missing 'suggestions' list")

    cleaned: list[dict] = []
    dropped_targets: list[str] = []
    for s in suggestions:
        if not isinstance(s, dict):
            continue
        # Network and storage variants use slightly different target
        # field names; check both.
        target_field = (
            "target_network_name" if "target_network_name" in s else "target_storage_class"
        )
        target_name = s.get(target_field)
        if target_name and target_name not in target_set:
            dropped_targets.append(target_name)
            s = {**s, target_field: None, "confidence": "low"}
        cleaned.append(s)

    rationale_summary = (body.get("rationale_summary") or "").strip()
    if dropped_targets:
        rationale_summary = (
            rationale_summary
            + (
                f" Dropped {len(dropped_targets)} hallucinated target name(s) "
                f"({', '.join(sorted(set(dropped_targets))[:3])}…); review the "
                "low-confidence rows manually."
            )
        ).strip()
    return {"suggestions": cleaned, "rationale_summary": rationale_summary}
