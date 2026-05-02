"""Validation orchestrator — owns the prompt + verdict schema + diff engine.

Transport is delegated to an ``LLMBackend`` instance so the same
prompt works against Ollama, KServe, or any future backend. The
public surface (``LLMClient.validate``) is unchanged from the
pre-refactor module so existing call sites keep working.
"""

from __future__ import annotations

import json
from typing import Any

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.factory import get_llm_backend


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
commentary outside the JSON."""


class LLMClient:
    """Validation orchestrator.

    Construct without arguments to use the configured backend (factory
    pattern). Tests pass an explicit ``backend`` to inject a stub.
    """

    def __init__(self, backend: LLMBackend | None = None) -> None:
        self.backend = backend or get_llm_backend()

    def validate(self, baseline: dict, current_state: dict, vm_role: str) -> dict:
        """Reason over pre/post migration diff and return a structured verdict."""
        diff = self._diff_state(baseline, current_state)
        # The OS profile is captured at baseline time; surface it in the
        # prompt so the LLM uses Windows terminology for Windows VMs and
        # Linux terminology for Linux VMs without us having to maintain
        # per-OS prompt templates.
        os_profile = (baseline.get("meta") or {}).get("os_profile") or {}
        user_prompt = self._render_prompt(vm_role, os_profile, diff)
        try:
            response = self.backend.chat_sync(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
            )
        except LLMBackendError as e:
            raise LLMError(str(e)) from e
        verdict = self._parse_verdict(response.get("content", ""))
        verdict["diff"] = diff
        return verdict

    @staticmethod
    def _parse_verdict(raw: str) -> dict:
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

        if not isinstance(verdict["findings"], list):
            raise LLMError("findings must be a list")
        if not isinstance(verdict["remediation"], list):
            raise LLMError("remediation must be a list")

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
        return {
            "services": _diff_services(
                baseline.get("services", []), current.get("services", [])
            ),
            "ports": _diff_ports(baseline.get("ports", []), current.get("ports", [])),
            "mounts": _diff_mounts(baseline.get("mounts", []), current.get("mounts", [])),
            "network": _diff_network(baseline.get("network", {}), current.get("network", {})),
            "cron": _diff_cron(baseline.get("cron", {}), current.get("cron", {})),
        }


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
