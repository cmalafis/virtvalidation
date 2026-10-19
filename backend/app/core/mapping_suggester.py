"""Suggestion engine for resource mappings — rules first, LLM for the rest.

Given the source vSphere networks / datastores in a mapping's vCenter
scope and the target cluster's catalog, propose a target for every
source with a confidence and a ``method`` saying how it was decided.
Operators review every row in the mapping editor; nothing auto-commits.

Per the LLM input discipline in CLAUDE.md:

1. **Deterministic pass** (pure Python). A source portgroup whose VLAN
   (from the RVTools vPort / dvPort sheets, or a number in its name)
   equals exactly one catalog network's VLAN is ``rule:vlan`` / high.
   A normalized-name match is ``rule:name`` / medium. A cluster with a
   single StorageClass maps every datastore to it (``rule:single_target``).
2. **LLM pass** for what is left — the genuinely ambiguous names.
   Sources go in batches of at most ``llm_max_items_per_call`` (10), each
   with a shortlist of at most 10 candidates ranked by the Python scorer,
   plus a few lines on what the target OpenShift release supports
   (:func:`app.core.platform_features.prompt_context`).
3. **Validate** every LLM answer: a JSON object, every batch source
   answered exactly once, every target drawn from the shortlist.
   **Retry** up to 3 times with the validation error as the first line
   of the next prompt, then **fall back** to the scorer's best candidate
   (``mechanical_fallback``, low confidence) — the operator always gets
   a full set of rows.

Auth and guardrail failures fall back too, and also set the operator
banner (``mechanical_fallback_auth`` / ``mechanical_fallback_guardrail``).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from app.core.config import settings
from app.core.llm.base import LLMAuthError, LLMBackend, LLMBackendError, LLMGuardrailError
from app.core.llm.status import clear_last_llm_error, record_last_llm_error

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_CANDIDATES_PER_CALL = 10

# The mock backend keys its canned response on this phrase.
_TASK_LINE = "Map each source to one candidate from the shortlist."

NETWORK_SYSTEM_PROMPT = """You are an OpenShift Virtualization migration engineer.
You map VMware vSphere port groups onto networks that already exist on the
target OpenShift cluster. You receive a few SOURCES and a SHORTLIST of
candidate target networks; every candidate was declared by the operator.

How to choose:
- A matching VLAN ID is the strongest signal; then a name that names the same
  segment, tier or zone (dmz, db, mgmt, backup, prod/dev).
- type/role/topology describe the candidate: a primary UDN/CUDN replaces the
  pod network for its namespace (VMs keep their IP across live migration); a
  localnet or bridge network puts the VM directly on a physical VLAN; "pod" is
  the cluster default network (NAT, no external L2).
- A management, vMotion, storage or backup port group often has no VM-facing
  equivalent: answer target null rather than force a match.
- Never invent a target. Use only names from the SHORTLIST, or null.

Reply with ONLY a JSON object:
{"suggestions": [{"source": "<source name>", "target": "<candidate name or null>",
  "confidence": "high|medium|low", "rationale": "<one sentence>"}]}
Answer every source exactly once."""

STORAGE_SYSTEM_PROMPT = """You are an OpenShift Virtualization migration engineer.
You map VMware vSphere datastores onto StorageClasses that already exist on the
target OpenShift cluster. You receive a few SOURCES and a SHORTLIST of
candidate StorageClasses; every candidate was declared by the operator.

How to choose:
- Match the performance tier the datastore name implies (gold/ssd/nvme/flash/
  tier1 vs silver/bronze/sata/archive) to the candidate's name and provisioner.
- VMs that must live-migrate need ReadWriteMany; Block volume mode is preferred
  for VM disks when the provisioner offers it (e.g. Ceph RBD).
- Prefer the cluster default (is_default) when nothing else distinguishes.
- Never invent a target. Use only names from the SHORTLIST, or null.

Reply with ONLY a JSON object:
{"suggestions": [{"source": "<source name>", "target": "<candidate name or null>",
  "confidence": "high|medium|low", "rationale": "<one sentence>"}]}
Answer every source exactly once."""


class SuggestionError(RuntimeError):
    """Kept for callers that still catch it; the engine no longer raises it
    for LLM trouble — it falls back instead."""


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------
_NOISE = {
    "vlan", "pg", "dpg", "dvpg", "dvs", "vds", "vss", "portgroup", "port", "group",
    "net", "network", "nad", "udn", "cudn", "vm", "vms", "ds", "datastore", "sc",
    "storage", "class", "csi", "the",
}  # fmt: skip
_TOKEN_RE = re.compile(r"[a-z]+|\d+")
_VLAN_IN_NAME_RE = re.compile(r"(?:vlan|vl|v)[-_ ]?(\d{1,4})\b", re.IGNORECASE)


def _tokens(name: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((name or "").lower()) if t not in _NOISE}


def _norm(name: str) -> str:
    return "".join(sorted(_tokens(name)))


def vlan_from_name(name: str) -> int | None:
    m = _VLAN_IN_NAME_RE.search(name or "")
    if not m:
        return None
    v = int(m.group(1))
    return v if 0 < v <= 4094 else None


def _name_score(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _source_vlan(src: dict) -> int | None:
    v = src.get("vlan_id")
    return v if isinstance(v, int) and v > 0 else vlan_from_name(src.get("name") or "")


def _target_vlan(t: dict) -> int | None:
    v = t.get("vlan_id")
    return v if isinstance(v, int) and v > 0 else vlan_from_name(t.get("name") or "")


def _network_score(src: dict, t: dict) -> float:
    s = _name_score(src["name"], t["name"])
    sv, tv = _source_vlan(src), _target_vlan(t)
    if sv is not None and sv == tv:
        s += 1.0
    return s


def _storage_score(src: dict, t: dict) -> float:
    s = _name_score(src["name"], t["name"])
    if t.get("is_default"):
        s += 0.05  # tie-break only
    return s


@dataclass
class _Row:
    source: str
    target: str | None
    confidence: str | None
    rationale: str
    method: str


def _network_rules(src: dict, targets: list[dict]) -> _Row | None:
    sv = _source_vlan(src)
    if sv is not None:
        same = [t for t in targets if _target_vlan(t) == sv]
        if len(same) == 1:
            return _Row(src["name"], same[0]["name"], "high", f"Same VLAN ({sv}).", "rule:vlan")
        if len(same) > 1:
            best = max(same, key=lambda t: _name_score(src["name"], t["name"]))
            return _Row(
                src["name"],
                best["name"],
                "medium",
                f"VLAN {sv} is on {len(same)} target networks; closest name chosen.",
                "rule:vlan",
            )
    key = _norm(src["name"])
    if key:
        same = [t for t in targets if _norm(t["name"]) == key]
        if len(same) == 1:
            return _Row(src["name"], same[0]["name"], "medium", "Same name.", "rule:name")
    return None


def _storage_rules(src: dict, targets: list[dict]) -> _Row | None:
    if len(targets) == 1:
        return _Row(
            src["name"],
            targets[0]["name"],
            "medium",
            "The cluster declares a single StorageClass.",
            "rule:single_target",
        )
    key = _norm(src["name"])
    if key:
        same = [t for t in targets if _norm(t["name"]) == key]
        if len(same) == 1:
            return _Row(src["name"], same[0]["name"], "medium", "Same name.", "rule:name")
    return None


def _fallback_row(
    src: dict,
    targets: list[dict],
    score: Callable[[dict, dict], float],
    method: str,
    *,
    default_ok: bool,
) -> _Row:
    """Best-scoring candidate, low confidence. For storage, the default
    StorageClass when nothing scores; for networks, no target (never
    guess a network — a wrong segment applies cleanly and lands the VM
    in the wrong security zone)."""
    ranked = sorted(targets, key=lambda t: -score(src, t))
    if ranked and score(src, ranked[0]) > 0.1:
        return _Row(src["name"], ranked[0]["name"], "low", "Closest name (automatic).", method)
    if default_ok:
        default = next((t for t in targets if t.get("is_default")), None)
        if default is not None:
            return _Row(
                src["name"], default["name"], "low", "Cluster default StorageClass.", method
            )
    return _Row(src["name"], None, None, "No confident match — choose by hand.", method)


# ---------------------------------------------------------------------------
# LLM pass
# ---------------------------------------------------------------------------
def _shortlist(batch: list[dict], targets: list[dict], score) -> list[dict]:
    """≤10 candidates: each source's best ones, round-robin, then the rest."""
    per_source = [sorted(targets, key=lambda t, s=s: -score(s, t)) for s in batch]
    picked: list[dict] = []
    seen: set[str] = set()
    depth = 0
    while len(picked) < MAX_CANDIDATES_PER_CALL and depth < len(targets):
        for ranked in per_source:
            if depth < len(ranked) and ranked[depth]["name"] not in seen:
                picked.append(ranked[depth])
                seen.add(ranked[depth]["name"])
                if len(picked) >= MAX_CANDIDATES_PER_CALL:
                    break
        depth += 1
    return picked


def _render_prompt(
    batch: list[dict],
    shortlist: list[dict],
    platform_lines: list[str],
    error_feedback: str | None,
) -> str:
    lines: list[str] = []
    if error_feedback:
        lines.append(f"YOUR PREVIOUS ANSWER WAS REJECTED: {error_feedback}")
        lines.append("")
    lines.append(_TASK_LINE)
    lines.extend(platform_lines)
    lines.append("")
    lines.append("SOURCES_JSON: " + json.dumps(batch, separators=(",", ":")))
    lines.append("SHORTLIST_JSON: " + json.dumps(shortlist, separators=(",", ":")))
    return "\n".join(lines)


def _strip_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


def _parse(raw: str, batch_names: list[str], shortlist_names: set[str]) -> dict[str, dict]:
    """source → {target, confidence, rationale}; raises ValueError with a
    message the model can act on."""
    try:
        body = json.loads(_strip_fences(raw))
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON ({e.msg}). Reply with only the JSON object.") from e
    if not isinstance(body, dict) or not isinstance(body.get("suggestions"), list):
        raise ValueError('expected {"suggestions": [...]}')
    out: dict[str, dict] = {}
    for s in body["suggestions"]:
        if not isinstance(s, dict):
            raise ValueError("every suggestion must be an object")
        src = s.get("source")
        if src not in batch_names:
            raise ValueError(f"{src!r} is not one of the SOURCES")
        if src in out:
            raise ValueError(f"{src!r} answered more than once")
        tgt = s.get("target")
        if tgt in ("", "null", "none"):
            tgt = None
        if tgt is not None and tgt not in shortlist_names:
            raise ValueError(
                f"target {tgt!r} for {src!r} is not in the SHORTLIST; use a listed name or null"
            )
        conf = s.get("confidence") if s.get("confidence") in ("high", "medium", "low") else "low"
        out[src] = {
            "target": tgt,
            "confidence": conf if tgt else None,
            "rationale": str(s.get("rationale") or "")[:300],
        }
    missing = [n for n in batch_names if n not in out]
    if missing:
        raise ValueError(f"missing answers for {missing}")
    return out


def _llm_batch(
    batch: list[dict],
    targets: list[dict],
    *,
    system_prompt: str,
    score,
    backend: LLMBackend | None,
    platform_lines: list[str],
    default_ok: bool,
    on_inference: Callable[[dict], None] | None,
) -> list[_Row]:
    shortlist = _shortlist(batch, targets, score)
    names = [s["name"] for s in batch]
    if backend is None:
        return [
            _fallback_row(s, targets, score, "mechanical_fallback", default_ok=default_ok)
            for s in batch
        ]
    shortlist_names = {t["name"] for t in shortlist}
    backend_type = getattr(backend, "backend_type", "unknown")
    error_feedback: str | None = None
    fallback_method = "mechanical_fallback"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": _render_prompt(batch, shortlist, platform_lines, error_feedback),
            },
        ]
        started = time.monotonic()
        try:
            response = backend.chat_sync(messages=messages, temperature=0.1)
        except LLMAuthError:
            record_last_llm_error(
                f"{backend_type} authentication failed (HTTP 401/403). Mapping suggestions "
                "are using the mechanical fallback. Check the configured credentials."
            )
            fallback_method = "mechanical_fallback_auth"
            break
        except LLMGuardrailError as exc:
            record_last_llm_error(
                f"{backend_type} guardrail detector flagged a mapping-suggestion prompt/"
                f"response. Suggestions are using the mechanical fallback. {exc}"
            )
            fallback_method = "mechanical_fallback_guardrail"
            break
        except LLMBackendError as exc:
            logger.warning("mapping_suggest.transport_failed attempt=%d error=%s", attempt, exc)
            break
        raw = response.get("content") or ""
        method = "llm" if attempt == 1 else f"llm_retry_{attempt - 1}"
        try:
            parsed = _parse(raw, names, shortlist_names)
        except ValueError as exc:
            error_feedback = str(exc)
            logger.info("mapping_suggest.parse_failed attempt=%d error=%s", attempt, exc)
            if on_inference:
                on_inference(
                    {
                        "backend_type": backend_type,
                        "model": response.get("model") or "",
                        "method": "llm_rejected",
                        "messages": messages,
                        "output": raw,
                        "latency_ms": int((time.monotonic() - started) * 1000),
                    }
                )
            continue
        clear_last_llm_error()
        if on_inference:
            on_inference(
                {
                    "backend_type": backend_type,
                    "model": response.get("model") or "",
                    "method": method,
                    "messages": messages,
                    "output": raw,
                    "latency_ms": int((time.monotonic() - started) * 1000),
                }
            )
        return [
            _Row(n, parsed[n]["target"], parsed[n]["confidence"], parsed[n]["rationale"], method)
            for n in names
        ]
    return [_fallback_row(s, targets, score, fallback_method, default_ok=default_ok) for s in batch]


def _suggest(
    *,
    sources: list[dict],
    targets: list[dict],
    rules: Callable[[dict, list[dict]], _Row | None],
    score: Callable[[dict, dict], float],
    system_prompt: str,
    backend: LLMBackend | None,
    platform_lines: list[str],
    default_ok: bool,
    on_inference: Callable[[dict], None] | None,
) -> list[_Row]:
    rows: dict[str, _Row] = {}
    leftovers: list[dict] = []
    for src in sources:
        hit = rules(src, targets)
        if hit is not None:
            rows[src["name"]] = hit
        else:
            leftovers.append(src)
    per_call = max(1, min(int(settings.llm_max_items_per_call), 10))
    for i in range(0, len(leftovers), per_call):
        batch = leftovers[i : i + per_call]
        for row in _llm_batch(
            batch,
            targets,
            system_prompt=system_prompt,
            score=score,
            backend=backend,
            platform_lines=platform_lines,
            default_ok=default_ok,
            on_inference=on_inference,
        ):
            rows[row.source] = row
    return [rows[s["name"]] for s in sources]


def _summary(rows: list[_Row], noun: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.method] = counts.get(r.method, 0) + 1
    rules = sum(v for k, v in counts.items() if k.startswith("rule:"))
    llm = sum(v for k, v in counts.items() if k.startswith("llm"))
    fallback = sum(v for k, v in counts.items() if k.startswith("mechanical"))
    unmatched = sum(1 for r in rows if not r.target)
    parts = [f"{rules} by rule (VLAN / name)", f"{llm} by the LLM"]
    if fallback:
        parts.append(f"{fallback} by the automatic fallback")
    text = f"{len(rows)} {noun}: " + ", ".join(parts) + "."
    if unmatched:
        text += f" {unmatched} have no confident match — choose those by hand."
    return text, counts


def suggest_network_mappings(
    *,
    sources: list[dict],
    targets: list[dict],
    backend: LLMBackend | None = None,
    platform_lines: list[str] | None = None,
    on_inference: Callable[[dict], None] | None = None,
) -> dict:
    """``sources``: ``[{"name", "vm_count", "vlan_id"?, "switch"?}]``.
    ``targets``: ``[{"name", "type", "namespace", "role"?, "topology"?, "vlan_id"?}]``."""
    if not sources:
        return {"suggestions": [], "rationale_summary": "No source networks to map.", "methods": {}}
    if not targets:
        return {
            "suggestions": [],
            "rationale_summary": "No target networks declared for this cluster.",
            "methods": {},
        }
    rows = _suggest(
        sources=sources,
        targets=targets,
        rules=_network_rules,
        score=_network_score,
        system_prompt=NETWORK_SYSTEM_PROMPT,
        backend=backend,
        platform_lines=platform_lines or [],
        default_ok=False,
        on_inference=on_inference,
    )
    by_name = {t["name"]: t for t in targets}
    suggestions = [
        {
            "source_network": r.source,
            "target_network_name": r.target,
            "target_network_type": (by_name.get(r.target) or {}).get("type") if r.target else None,
            "target_namespace": (by_name.get(r.target) or {}).get("namespace")
            if r.target
            else None,
            "confidence": r.confidence,
            "rationale": r.rationale or None,
            "method": r.method,
        }
        for r in rows
    ]
    text, counts = _summary(rows, "networks")
    return {"suggestions": suggestions, "rationale_summary": text, "methods": counts}


def suggest_storage_mappings(
    *,
    sources: list[dict],
    targets: list[dict],
    backend: LLMBackend | None = None,
    platform_lines: list[str] | None = None,
    on_inference: Callable[[dict], None] | None = None,
) -> dict:
    """``sources``: ``[{"name", "vm_count"}]``. ``targets``: ``[{"name",
    "provisioner", "access_mode", "volume_mode", "is_default"}]``."""
    if not sources:
        return {
            "suggestions": [],
            "rationale_summary": "No source datastores to map.",
            "methods": {},
        }
    if not targets:
        return {
            "suggestions": [],
            "rationale_summary": "No target StorageClasses declared for this cluster.",
            "methods": {},
        }
    rows = _suggest(
        sources=sources,
        targets=targets,
        rules=_storage_rules,
        score=_storage_score,
        system_prompt=STORAGE_SYSTEM_PROMPT,
        backend=backend,
        platform_lines=platform_lines or [],
        default_ok=True,
        on_inference=on_inference,
    )
    suggestions = [
        {
            "source_datastore": r.source,
            "target_storage_class": r.target,
            "access_mode": None,
            "confidence": r.confidence,
            "rationale": r.rationale or None,
            "method": r.method,
        }
        for r in rows
    ]
    text, counts = _summary(rows, "datastores")
    return {"suggestions": suggestions, "rationale_summary": text, "methods": counts}
