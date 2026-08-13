"""Validation orchestrator — owns the prompt + verdict schema + diff engine.

Transport is delegated to an ``LLMBackend`` instance so the same
prompt works against Ollama, KServe, or any future backend. The
public surface (``LLMClient.validate``) is unchanged from the
pre-refactor module so existing call sites keep working.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from app.core.llm.base import LLMAuthError, LLMBackend, LLMBackendError, LLMGuardrailError
from app.core.llm.runtime import get_active_backend
from app.core.llm.status import clear_last_llm_error, record_last_llm_error

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """Raised when the LLM call fails or returns unparseable output.

    Subclasses ``RuntimeError`` so existing ``except LLMError`` clauses
    don't change. ``LLMBackendError`` from the transport layer is
    re-raised as ``LLMError`` to keep the validation flow's error
    surface stable.
    """


SYSTEM_PROMPT = """You are VirtValidate, an expert systems engineer validating a VM that
was migrated from VMware to OpenShift Virtualization. The VM may be
Linux (RHEL family, Debian/Ubuntu) or Windows Server (2019/2022/2025).
Compare the pre-migration baseline against the current state and
identify any meaningful differences that indicate the migration didn't
preserve the workload's expected behavior.

You will receive:
  - the VM's role / OS profile / hostname (OS family is given explicitly)
  - the pre-migration baseline profile (services, network, ports, mounts,
    cron-or-scheduled-tasks; on Windows VMs, additional hotfixes and
    AD-membership blocks)
  - the post-migration current state (same shape)
  - a precomputed structured diff highlighting added / removed / changed items

Use platform-appropriate terminology in your findings:
  - Linux: systemd services, cron jobs, mounts, iptables/nftables
  - Windows: Windows services, scheduled tasks, drives/volumes,
    Windows Firewall, AD membership, installed hotfixes
Do not say "cron job missing" for a Windows VM, or "scheduled task drift"
for a Linux VM — match the OS the VM actually runs.

Severity guidance — use this exactly:

  CRITICAL: services that were running but aren't, missing network
    interfaces, missing mounts, missing application processes that the
    role depends on. Anything that means production traffic would fail.

  HIGH: configuration drift on important services, missing cron jobs
    that affect data integrity, ports the role normally listens on
    that disappeared, performance degradation > 50%.

  MEDIUM: minor config differences, unexpected services running, mount
    options that drifted, performance variance within a reasonable
    range. Worth knowing about but not blocking.

  LOW: expected migration artifacts (different uptime, new PIDs, kernel
    patch level, refreshed ssh host key). These should generally be
    skipped, not surfaced — but include if you're not sure.

Ignore expected post-migration differences:
  - timestamps, uptime, kernel patch level
  - PID changes, new ssh host key fingerprints
  - DHCP-assigned IPs that match the same subnet
  - cosmetic service churn (logrotate, accounts-daemon, etc.)

Verdict mapping:
  - "pass"  → no findings worse than LOW
  - "warn"  → one or more MEDIUM/HIGH findings, but no CRITICAL
  - "fail"  → at least one CRITICAL finding

Respond with a SINGLE JSON object and nothing else. Schema:

{
  "status": "pass" | "warn" | "fail",
  "summary": "two-or-three-sentence plain-English verdict suitable for a CIO",
  "findings": [
    {
      "severity": "critical" | "high" | "medium" | "low" | "info",
      "category": "services" | "network" | "ports" | "mounts" | "cron" | "other",
      "title": "<short headline, < 100 chars>",
      "description": "<plain-English explanation, multi-sentence ok>",
      "source_evidence": "<exact quote / reference from the baseline (or '(absent)')>",
      "current_evidence": "<exact quote / reference from the current state (or '(absent)')>",
      "remediation": "<actionable next step the operator should take>",
      "confidence": "high" | "medium" | "low"
    }
  ],
  "remediation": [
    {
      "step": 1,
      "action": "<top-level remediation summary; per-finding remediation lives inside findings[]>",
      "command": "<exact shell command, or null>"
    }
  ]
}

Order findings most-critical first. Mark "low" confidence whenever the
diff is ambiguous (e.g. a service replaced with what looks like a
renamed but functionally-equivalent service). No markdown fences. No
commentary outside the JSON.

## Example outputs

These are the shape and depth of analysis we expect. Three flavors:

### Example A — critical issue, correctly flagged

Diff (excerpt):
  services.removed = ["postgresql.service", "redis.service"]
  services.added = ["kubevirt-agent.service"]
  ports.removed = [{proto:"tcp", port:5432, address:"0.0.0.0"}]

Verdict:
{
  "status": "fail",
  "summary": "Production database services (PostgreSQL, Redis) are not running. The 5432 port that the database normally listens on has disappeared. Migration validated successfully for OS-level state but the database workload itself is offline and requires immediate intervention.",
  "findings": [
    {
      "severity": "critical",
      "category": "services",
      "title": "PostgreSQL service is not running",
      "description": "postgresql.service was active in the baseline snapshot and is absent from the current state. Port 5432 has also disappeared, confirming the database itself isn't accepting connections.",
      "source_evidence": "services[].unit == 'postgresql.service' (active, running) in baseline",
      "current_evidence": "postgresql.service absent from current services list; tcp:5432 not in ports",
      "remediation": "systemctl status postgresql; check journalctl -u postgresql for startup failure. Common causes: missing pg_hba.conf, data directory permissions, SELinux booleans not migrated.",
      "confidence": "high"
    },
    {
      "severity": "critical",
      "category": "services",
      "title": "Redis service is not running",
      "description": "redis.service was active in the baseline snapshot and is absent post-migration.",
      "source_evidence": "services[].unit == 'redis.service' in baseline",
      "current_evidence": "redis.service absent from current services list",
      "remediation": "systemctl start redis; check journal for the failure root cause.",
      "confidence": "high"
    }
  ],
  "remediation": [
    {"step": 1, "action": "Restart database tier", "command": "systemctl start postgresql redis"},
    {"step": 2, "action": "Verify on app tier", "command": null}
  ]
}

### Example B — expected post-migration change, correctly NOT flagged as concerning

Diff (excerpt):
  services.added = ["kubevirt-agent.service", "qemu-guest-agent.service"]
  network.interfaces = {"eth0":{ipv4_removed:["10.0.0.5"]}, "ens192":{ipv4_added:["10.0.0.5"]}}

Verdict:
{
  "status": "pass",
  "summary": "Migration completed cleanly. The kubevirt-agent and qemu-guest-agent services were added as expected (MTV conversion step). The eth0 → ens192 interface rename is the standard VirtIO naming switch on RHEL guests and preserves the same IP address.",
  "findings": [
    {
      "severity": "info",
      "category": "network",
      "title": "Network interface renamed eth0 → ens192 (expected)",
      "description": "VirtIO networking on KubeVirt produces predictable ens* interface names. The IP address 10.0.0.5 transferred to the new interface without change.",
      "source_evidence": "eth0 with 10.0.0.5/24 in baseline",
      "current_evidence": "ens192 with 10.0.0.5/24 in current state",
      "remediation": "No action required. Verify application configs don't pin the interface name.",
      "confidence": "high"
    }
  ],
  "remediation": []
}

### Example C — ambiguous case, low confidence with uncertainty

Diff (excerpt):
  services.added = ["myapp-worker.service"]
  services.removed = ["app-worker.service"]

Verdict:
{
  "status": "warn",
  "summary": "An application service appears to have been renamed (app-worker → myapp-worker). The naming change is plausible but I cannot verify the new service actually provides the same functionality without inspecting the unit file.",
  "findings": [
    {
      "severity": "medium",
      "category": "services",
      "title": "Application service may have been renamed",
      "description": "app-worker.service is no longer present and myapp-worker.service appeared. The renamed service is plausibly the same workload but the rename was not done by the standard migration toolchain.",
      "source_evidence": "app-worker.service active in baseline",
      "current_evidence": "app-worker.service absent; myapp-worker.service present",
      "remediation": "Verify myapp-worker provides the same workload (compare ExecStart lines, port bindings). If equivalent, document the rename and acknowledge this finding.",
      "confidence": "low"
    }
  ],
  "remediation": [
    {"step": 1, "action": "Compare service unit files", "command": "systemctl cat myapp-worker | diff - <(systemctl cat app-worker)"}
  ]
}

Use these examples for shape and depth — do NOT copy their text verbatim. Your actual analysis must reflect the diff in this specific request."""


class LLMClient:
    """Validation orchestrator.

    Construct without arguments to use the configured backend (factory
    pattern). Tests pass an explicit ``backend`` to inject a stub.
    """

    def __init__(self, backend: LLMBackend | None = None) -> None:
        self.backend = backend or get_active_backend()

    def validate(
        self,
        baseline: dict,
        current_state: dict,
        vm_role: str,
        *,
        max_retries: int = 1,
        capture: Optional[dict] = None,
    ) -> dict:
        """Reason over pre/post migration diff and return a structured verdict.

        On structural validation failure (missing evidence fields,
        invalid status, bad JSON) the call retries once with a
        targeted feedback message explaining what was wrong. If the
        retry also fails, the verdict is marked ``manual_review`` so
        the bulk pipeline can flag it for an operator without
        blocking other VMs.

        ``capture`` is an optional out-dict the caller can pass to receive
        the exact ``messages`` sent, the raw ``raw_response`` text, and the
        ``method`` taken (``llm`` / ``llm_retry_N`` / ``manual_review``) for
        the InferenceLog audit row. It's populated on every non-raising
        return path so the orchestrator can persist what the model saw and
        produced without the verdict dict (which gets cached) carrying it.
        """
        diff = self._diff_state(baseline, current_state)
        # The OS profile is captured at baseline time; surface it in the
        # prompt so the LLM uses Windows terminology for Windows VMs and
        # Linux terminology for Linux VMs without us having to maintain
        # per-OS prompt templates.
        os_profile = (baseline.get("meta") or {}).get("os_profile") or {}
        user_prompt = self._render_prompt(vm_role, os_profile, diff)

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        last_validation_error: Optional[str] = None
        last_raw: str = ""
        for attempt in range(max_retries + 1):
            if attempt > 0 and last_validation_error:
                # Append the previous reply + a corrective message so
                # the model can see what went wrong and produce a
                # well-formed response on the retry.
                messages = messages + [
                    {"role": "assistant", "content": last_raw or "(empty)"},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was rejected by the validator:\n"
                            f"  {last_validation_error}\n"
                            "Please re-emit the JSON object honoring the schema in the "
                            "system prompt. Every finding MUST include non-empty "
                            "`source_evidence`, `current_evidence`, `remediation`, and "
                            "`confidence`. Output a single JSON object only — no markdown, "
                            "no commentary."
                        ),
                    },
                ]
            try:
                response = self.backend.chat_sync(messages=messages, temperature=0.1)
            except LLMAuthError as e:
                # Auth failure during validation — surface to the
                # Settings UI banner. Re-raised as LLMError so the
                # validation orchestrator's existing error envelope
                # path takes over. The banner is the SECONDARY signal
                # operators rely on; the validation flow's error
                # message is the primary.
                record_last_llm_error(
                    f"{getattr(self.backend, 'backend_type', 'LLM')} "
                    f"authentication failed (HTTP 401/403) during "
                    f"validation. Check the configured credentials."
                )
                raise LLMError(str(e)) from e
            except LLMGuardrailError as e:
                # A guardrail detector flagged the input or output. Asymmetric
                # like an auth failure: surface to the operator banner AND
                # complete the flow via a manual-review verdict (the
                # validate-retry-fallback rule — never a 5xx because a detector
                # fired). The flagged content is NOT cached.
                record_last_llm_error(
                    f"{getattr(self.backend, 'backend_type', 'LLM')} guardrail "
                    f"detector flagged a validation prompt/response. The result "
                    f"was routed to manual review. {e}"
                )
                if capture is not None:
                    capture["messages"] = messages
                    capture["raw_response"] = last_raw
                    capture["method"] = "mechanical_fallback_guardrail"
                    capture["detections"] = getattr(e, "detections", {})
                return {
                    "status": "warn",
                    "summary": (
                        "A TrustyAI guardrail detector flagged this validation "
                        f"exchange ({e}). Routed to manual operator review."
                    ),
                    "findings": [],
                    "remediation": [],
                    "confidence": "low",
                    "diff": diff,
                    "model": "",
                    "needs_manual_review": True,
                }
            except LLMBackendError as e:
                raise LLMError(str(e)) from e
            last_raw = response.get("content", "")
            try:
                verdict = self._parse_verdict(last_raw)
                verdict["diff"] = diff
                verdict["model"] = response.get("model") or ""
                verdict["needs_manual_review"] = False
                # Successful LLM call — clear any stale auth banner.
                clear_last_llm_error()
                if capture is not None:
                    capture["messages"] = messages
                    capture["raw_response"] = last_raw
                    capture["method"] = "llm" if attempt == 0 else f"llm_retry_{attempt}"
                return verdict
            except LLMError as e:
                last_validation_error = str(e)
                logger.warning(
                    "Validation LLM output rejected on attempt %d/%d: %s",
                    attempt + 1,
                    max_retries + 1,
                    e,
                )

        # All retries exhausted — surface a "manual review" verdict
        # rather than failing the whole bulk run.
        if capture is not None:
            capture["messages"] = messages
            capture["raw_response"] = last_raw
            capture["method"] = "manual_review"
        return {
            "status": "warn",
            "summary": (
                "LLM output did not match the required schema after retry. "
                f"Last validator error: {last_validation_error}. Manual "
                "operator review required."
            ),
            "findings": [],
            "remediation": [],
            "confidence": "low",
            "diff": diff,
            "model": "",
            "needs_manual_review": True,
        }

    @staticmethod
    def _parse_verdict(raw: str) -> dict:
        """Parse + structurally validate the LLM output.

        Validation rules — every one of these is a production-day bug
        if the LLM gets it wrong, so we reject hard and let the retry
        loop give the model another shot at producing a well-formed
        response:

          - JSON must be well-formed.
          - Top level is an object with status / summary / findings /
            remediation keys.
          - status ∈ {pass, warn, fail}.
          - Every finding has non-empty source_evidence + current_evidence +
            remediation + confidence + severity.
          - Verdict status matches the worst finding's severity:
              fail   ⇒ at least one critical finding
              warn   ⇒ at least one high/medium finding (and no critical)
              pass   ⇒ no critical/high/medium findings
        """
        try:
            verdict = json.loads(raw)
        except json.JSONDecodeError as e:
            raise LLMError(f"Model output was not valid JSON: {e}\n{raw[:500]}") from e

        if not isinstance(verdict, dict):
            raise LLMError("Model output was not a JSON object")

        status = verdict.get("status")
        if status not in {"pass", "warn", "fail"}:
            raise LLMError(f"Invalid verdict status: {status!r}")

        verdict.setdefault("summary", "")
        verdict.setdefault("findings", [])
        verdict.setdefault("remediation", [])
        verdict.setdefault("confidence", "medium")

        if not isinstance(verdict["findings"], list):
            raise LLMError("findings must be a list")
        if not isinstance(verdict["remediation"], list):
            raise LLMError("remediation must be a list")

        allowed_severity = {"critical", "high", "medium", "low", "info"}
        allowed_confidence = {"high", "medium", "low"}
        worst_rank = 0
        rank = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}

        for idx, finding in enumerate(verdict["findings"]):
            if not isinstance(finding, dict):
                raise LLMError(f"findings[{idx}] is not an object")
            severity = (finding.get("severity") or "").lower()
            if severity not in allowed_severity:
                raise LLMError(
                    f"findings[{idx}].severity must be one of {sorted(allowed_severity)}, "
                    f"got {severity!r}"
                )
            confidence = (finding.get("confidence") or "").lower()
            if confidence not in allowed_confidence:
                raise LLMError(
                    f"findings[{idx}].confidence must be one of {sorted(allowed_confidence)}, "
                    f"got {confidence!r}"
                )
            for required in ("source_evidence", "current_evidence", "remediation", "title"):
                value = (
                    (finding.get(required) or "").strip()
                    if isinstance(finding.get(required), str)
                    else finding.get(required)
                )
                if not value:
                    raise LLMError(f"findings[{idx}].{required} is required and must be non-empty")
            worst_rank = max(worst_rank, rank[severity])

        # Verdict-vs-findings consistency. The model is occasionally
        # over-confident in its summary; if it says "pass" but emitted
        # a critical finding, the parser overrides the verdict to fail.
        if worst_rank >= rank["critical"] and status != "fail":
            raise LLMError(
                "verdict.status must be 'fail' when at least one finding " "has severity 'critical'"
            )
        if status == "pass" and worst_rank >= rank["medium"]:
            raise LLMError(
                "verdict.status must be 'warn' or 'fail' when any finding "
                "has severity 'medium' or higher"
            )

        return verdict

    @staticmethod
    def _render_prompt(vm_role: str, os_profile: dict, diff: dict) -> str:
        family = (os_profile.get("distro_family") or "unknown").lower()
        distro = os_profile.get("distro") or "unknown"
        version = os_profile.get("major_version") or 0
        minor = os_profile.get("minor_version") or 0
        version_label = f"{version}.{minor}" if minor else str(version)
        pretty = os_profile.get("pretty_name") or ""

        # Family-specific terminology hint. Keeps the system prompt
        # generic and lets each request remind the model which side of
        # the fence this particular VM is on.
        if family == "windows":
            term_hint = (
                "This is a Windows VM — use Windows terminology in your findings:\n"
                "  - Windows services (NOT systemd units)\n"
                "  - scheduled tasks (NOT cron jobs)\n"
                "  - drives/volumes (NOT mountpoints)\n"
                "  - Windows Firewall (NOT iptables/nftables)\n"
                "  - if AD membership or hotfix blocks are present, factor them in.\n"
            )
        elif family in {"rhel-like", "debian-like"}:
            term_hint = (
                "This is a Linux VM — use Linux terminology in your findings:\n"
                "  - systemd services / units\n"
                "  - cron jobs (system + per-user)\n"
                "  - mountpoints + filesystems\n"
                "  - iptables/nftables firewall\n"
            )
        else:
            term_hint = (
                "OS family was not detected. Use generic terminology and "
                "lower confidence on findings that depend on platform "
                "conventions.\n"
            )

        return (
            f"VM role: {vm_role or 'unspecified'}\n"
            f"OS: {pretty or distro} ({family}, version {version_label})\n\n"
            f"{term_hint}\n"
            f"State diff (baseline -> current):\n"
            f"{json.dumps(diff, indent=2, sort_keys=True)}\n"
        )

    @staticmethod
    def _diff_state(baseline: dict, current: dict) -> dict:
        return compute_diff(baseline, current)


def compute_diff(baseline: dict, current: dict) -> dict:
    """Module-level alias for :meth:`LLMClient._diff_state`.

    Exposed for the tier classifier + bulk validation preview, which
    need the diff without instantiating an LLM client (and the
    expensive backend resolution that comes with it).
    """
    return {
        "services": _diff_services(baseline.get("services", []), current.get("services", [])),
        "ports": _diff_ports(baseline.get("ports", []), current.get("ports", [])),
        "mounts": _diff_mounts(baseline.get("mounts", []), current.get("mounts", [])),
        "network": _diff_network(baseline.get("network", {}), current.get("network", {})),
        "cron": _diff_cron(baseline.get("cron", {}), current.get("cron", {})),
    }


# Back-compat alias for callers that already import this name.
_diff_state_for_validation = compute_diff


# ---------------------------------------------------------------------------
# Diff helpers — pure functions kept here so test_llm.py can import them.
# ---------------------------------------------------------------------------
def _diff_services(before: list[dict], after: list[dict]) -> dict:
    before_units = {s.get("unit") for s in before if s.get("unit")}
    after_units = {s.get("unit") for s in after if s.get("unit")}
    return {
        "removed": sorted(before_units - after_units),
        "added": sorted(after_units - before_units),
    }


def _diff_ports(before: list[dict], after: list[dict]) -> dict:
    def key(p: dict) -> tuple:
        return (p.get("proto"), p.get("port"), p.get("address"))

    before_set = {key(p) for p in before}
    after_set = {key(p) for p in after}
    return {
        "removed": sorted(
            [{"proto": p, "port": port, "address": a} for (p, port, a) in before_set - after_set],
            key=lambda x: (str(x["proto"]), str(x["port"])),
        ),
        "added": sorted(
            [{"proto": p, "port": port, "address": a} for (p, port, a) in after_set - before_set],
            key=lambda x: (str(x["proto"]), str(x["port"])),
        ),
    }


def _diff_mounts(before: list[dict], after: list[dict]) -> dict:
    before_by_target: dict[str, dict] = {m["target"]: m for m in before if m.get("target")}
    after_by_target: dict[str, dict] = {m["target"]: m for m in after if m.get("target")}

    removed = sorted(set(before_by_target) - set(after_by_target))
    added = sorted(set(after_by_target) - set(before_by_target))
    changed = []
    for target in sorted(set(before_by_target) & set(after_by_target)):
        b, a = before_by_target[target], after_by_target[target]
        if b.get("source") != a.get("source") or b.get("fstype") != a.get("fstype"):
            changed.append({"target": target, "before": b, "after": a})
    return {"removed": removed, "added": added, "changed": changed}


def _diff_network(before: dict, after: dict) -> dict:
    before_ifaces = before.get("interfaces", {}) or {}
    after_ifaces = after.get("interfaces", {}) or {}

    iface_changes: dict[str, Any] = {}
    for iface in sorted(set(before_ifaces) | set(after_ifaces)):
        b = before_ifaces.get(iface, {"ipv4": [], "ipv6": []})
        a = after_ifaces.get(iface, {"ipv4": [], "ipv6": []})
        v4_removed = sorted(set(b.get("ipv4", [])) - set(a.get("ipv4", [])))
        v4_added = sorted(set(a.get("ipv4", [])) - set(b.get("ipv4", [])))
        v6_removed = sorted(set(b.get("ipv6", [])) - set(a.get("ipv6", [])))
        v6_added = sorted(set(a.get("ipv6", [])) - set(b.get("ipv6", [])))
        if v4_removed or v4_added or v6_removed or v6_added:
            iface_changes[iface] = {
                "ipv4_added": v4_added,
                "ipv4_removed": v4_removed,
                "ipv6_added": v6_added,
                "ipv6_removed": v6_removed,
            }

    before_routes = set(before.get("routes", []) or [])
    after_routes = set(after.get("routes", []) or [])
    before_dns = set(before.get("dns", []) or [])
    after_dns = set(after.get("dns", []) or [])

    return {
        "interfaces": iface_changes,
        "routes_added": sorted(after_routes - before_routes),
        "routes_removed": sorted(before_routes - after_routes),
        "dns_added": sorted(after_dns - before_dns),
        "dns_removed": sorted(before_dns - after_dns),
    }


def _diff_cron(before: dict, after: dict) -> dict:
    before_users = before.get("user_crontabs", {}) or {}
    after_users = after.get("user_crontabs", {}) or {}

    user_diff: dict[str, dict] = {}
    for user in sorted(set(before_users) | set(after_users)):
        b = set(before_users.get(user, []))
        a = set(after_users.get(user, []))
        if b != a:
            user_diff[user] = {
                "added": sorted(a - b),
                "removed": sorted(b - a),
            }

    before_sys = {
        item["path"]: set(item.get("entries", [])) for item in before.get("system", []) or []
    }
    after_sys = {
        item["path"]: set(item.get("entries", [])) for item in after.get("system", []) or []
    }
    system_diff: dict[str, dict] = {}
    for path in sorted(set(before_sys) | set(after_sys)):
        b = before_sys.get(path, set())
        a = after_sys.get(path, set())
        if b != a:
            system_diff[path] = {
                "added": sorted(a - b),
                "removed": sorted(b - a),
            }

    return {"users": user_diff, "system": system_diff}
