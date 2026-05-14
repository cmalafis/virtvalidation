"""Tiered validation classifier — decides whether a diff needs the LLM.

Three tiers, evaluated in order:

  - **Tier 1** (no LLM): the structured diff is empty or contains only
    benign categories the appliance already knows are uninteresting
    (timestamps, uptime, network rename patterns the rename table
    covers).
  - **Tier 2** (rules, no LLM): the diff matches deterministic rules
    that are clearly safe (a `kubevirt-agent` service appeared on a
    newly-migrated VM) or clearly unsafe (a `postgresql` service that
    was running pre-migration is now stopped). Returns a pre-formed
    verdict with rule-derived findings.
  - **Tier 3** (LLM): everything else. The diff goes to the LLM for
    semantic analysis. This is the existing code path; the tier
    classifier just decides whether to invoke it.

The goal is to skip the LLM in 60-80% of cases on a well-executed
migration. Tier 1 covers identical/trivial diffs; Tier 2 covers the
known patterns. The remainder is genuinely ambiguous and worth a
model call.

This module is pure Python and **never** imports an LLM backend. Any
test that hits the classifier proves the no-LLM path stays no-LLM.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Literal, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Knowledge bases — operator-tunable via the validation_rules module.
# Kept as module-level constants so tests can monkeypatch them.
# ---------------------------------------------------------------------------

# Services that legitimately appear on a freshly-migrated VM and should
# never trigger a finding by themselves. The KubeVirt agent + cloud-init
# are stamped in by MTV's conversion step; qemu-guest-agent is added
# automatically by the conversion process for Linux guests.
KNOWN_GOOD_NEW_SERVICES: frozenset[str] = frozenset(
    {
        "kubevirt-agent",
        "kubevirt-agent.service",
        "qemu-guest-agent",
        "qemu-guest-agent.service",
        "cloud-init",
        "cloud-init.service",
        "cloud-init-local.service",
        "cloud-config.service",
        "cloud-final.service",
        "virt-who",
        "virt-who.service",
    }
)

# Services whose disappearance on a production VM is always wrong.
# These are the load-bearing daemons — if one of them was running
# pre-migration and isn't running post-migration the migration is
# broken regardless of what else looks fine.
CRITICAL_SERVICES_FOR_PRODUCTION: frozenset[str] = frozenset(
    {
        # Web tier
        "httpd",
        "httpd.service",
        "nginx",
        "nginx.service",
        "apache2",
        "apache2.service",
        # Databases
        "postgresql",
        "postgresql.service",
        "postgresql-16.service",
        "mysql",
        "mysql.service",
        "mariadb",
        "mariadb.service",
        "mongod",
        "mongod.service",
        "redis",
        "redis.service",
        # App / runtime
        "tomcat",
        "tomcat.service",
        "java",
        "wildfly",
        "docker",
        "docker.service",
        "podman.service",
        "containerd.service",
        # Infrastructure
        "named",
        "named.service",  # BIND DNS
        "chronyd",
        "chronyd.service",
        "ntpd",
        "ntpd.service",
        "smbd",
        "nmbd",
        "winbind",
        # Identity
        "sssd",
        "sssd.service",
    }
)

# Network interface renames that routinely happen on conversion.
# vSphere VMs typically use eth0; KubeVirt's VirtIO interfaces show up
# as one of these depending on the guest's NetworkManager naming
# policy. A diff that only shows one of these renames isn't a real
# change.
KNOWN_NETWORK_RENAMES: list[tuple[str, str]] = [
    ("eth0", "enp0s3"),
    ("eth0", "ens3"),
    ("eth0", "ens192"),
    ("eth0", "ens18"),
    ("eth1", "enp0s8"),
    ("eth1", "ens4"),
]

# Diff sub-keys the classifier treats as "noise" for Tier 1.
# routes/dns changes on a network rename are a downstream artifact;
# we don't want to surface them when the rename itself is benign.
_IGNORED_DIFF_KEYS: frozenset[str] = frozenset(
    {"routes_added", "routes_removed", "dns_added", "dns_removed"}
)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------
TierLiteral = Literal["tier1", "tier2", "tier3"]
VerdictStatus = Literal["pass", "warn", "fail"]


@dataclass
class TierClassification:
    """Result of running a diff through the classifier.

    ``llm_required=False`` means tier 1 or 2 produced a verdict and the
    caller should skip the LLM call. ``verdict`` is populated only in
    that case — it's already in the shape the validation pipeline
    expects to persist.
    """

    tier: TierLiteral
    llm_required: bool
    rationale: str
    verdict: Optional[dict] = None
    matched_rules: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def _is_empty_section(section) -> bool:
    """A diff sub-section is empty when every list/dict inside is empty.

    Diff shape varies by category — services have removed/added,
    ports have removed/added, network has interfaces + 4 list keys.
    The classifier doesn't care which shape; it just needs to know
    whether anything changed.
    """
    if section is None:
        return True
    if isinstance(section, (list, str)):
        return len(section) == 0
    if isinstance(section, dict):
        return all(_is_empty_section(v) for v in section.values())
    return False


def _normalize_iface_changes(interfaces: dict) -> dict:
    """Drop interfaces that only differ by a known rename — the rename
    table already covers them.

    A rename shows up in the diff as one interface present in
    "removed" and another in "added", with the addresses transferred
    intact. We don't strictly verify address-set equality (that's
    nice-to-have); the presence of a known rename pair is enough to
    treat the change as benign.
    """
    if not interfaces:
        return {}
    rename_set: dict[str, str] = {}
    for old, new in KNOWN_NETWORK_RENAMES:
        rename_set[old] = new
        rename_set[new] = old

    matched_renames: set[str] = set()
    for iface in list(interfaces):
        change = interfaces[iface] or {}
        ipv4_added = change.get("ipv4_added") or []
        ipv4_removed = change.get("ipv4_removed") or []
        # A pure rename: the interface either fully gained or lost an
        # address set, and a partner interface is the inverse.
        if iface in rename_set:
            partner = rename_set[iface]
            partner_change = interfaces.get(partner)
            if partner_change is not None:
                p_added = partner_change.get("ipv4_added") or []
                p_removed = partner_change.get("ipv4_removed") or []
                # iface gained addresses partner lost (or vice versa)
                if (sorted(ipv4_added) == sorted(p_removed) and ipv4_added) or (
                    sorted(ipv4_removed) == sorted(p_added) and ipv4_removed
                ):
                    matched_renames.add(iface)
                    matched_renames.add(partner)

    return {k: v for k, v in interfaces.items() if k not in matched_renames}


def _services_changes(diff: dict) -> tuple[list[str], list[str]]:
    svcs = diff.get("services") or {}
    return list(svcs.get("removed") or []), list(svcs.get("added") or [])


def _network_changes(diff: dict) -> tuple[dict, list[str], list[str], list[str], list[str]]:
    net = diff.get("network") or {}
    return (
        _normalize_iface_changes(net.get("interfaces") or {}),
        list(net.get("routes_added") or []),
        list(net.get("routes_removed") or []),
        list(net.get("dns_added") or []),
        list(net.get("dns_removed") or []),
    )


def _ports_changes(diff: dict) -> tuple[list, list]:
    ports = diff.get("ports") or {}
    return list(ports.get("removed") or []), list(ports.get("added") or [])


def _mounts_changes(diff: dict) -> tuple[list, list, list]:
    mounts = diff.get("mounts") or {}
    return (
        list(mounts.get("removed") or []),
        list(mounts.get("added") or []),
        list(mounts.get("changed") or []),
    )


def _diff_is_effectively_empty(diff: dict) -> bool:
    """True when every non-ignored sub-section is empty after
    normalizing for known renames.

    "Effectively empty" includes diffs whose only network changes are
    rename-driven — those are noise the operator already accounts for.
    """
    services_removed, services_added = _services_changes(diff)
    if services_removed or services_added:
        return False
    ports_removed, ports_added = _ports_changes(diff)
    if ports_removed or ports_added:
        return False
    mounts_removed, mounts_added, mounts_changed = _mounts_changes(diff)
    if mounts_removed or mounts_added or mounts_changed:
        return False
    iface_changes, _, _, _, _ = _network_changes(diff)
    if iface_changes:
        return False
    cron = diff.get("cron") or {}
    if not _is_empty_section(cron):
        return False
    return True


# ---------------------------------------------------------------------------
# Tier evaluators
# ---------------------------------------------------------------------------
def _tier1(diff: dict) -> Optional[TierClassification]:
    if _diff_is_effectively_empty(diff):
        return TierClassification(
            tier="tier1",
            llm_required=False,
            rationale=(
                "Structured diff is empty (or contains only known network "
                "renames). Nothing material changed between baseline and "
                "current state."
            ),
            verdict={
                "status": "pass",
                "summary": "No material drift detected.",
                "findings": [],
                "remediation": [],
                "confidence": "high",
            },
            matched_rules=["tier1.empty_diff"],
        )
    return None


def _tier2(diff: dict, *, environment: str | None) -> Optional[TierClassification]:
    """Apply the deterministic-rule pass.

    Two flavors of decision:

      - **Clearly safe** — every change in the diff is on the
        known-good list. Pass without an LLM call.
      - **Clearly unsafe** — at least one critical service that was
        in the baseline disappeared post-migration. Fail without an
        LLM call; the LLM can't make this any clearer.

    Anything in between (some known-good + some unknown changes,
    or a non-critical service stopped) falls through to Tier 3
    so the LLM can reason about it.
    """
    services_removed, services_added = _services_changes(diff)
    ports_removed, ports_added = _ports_changes(diff)
    mounts_removed, mounts_added, mounts_changed = _mounts_changes(diff)
    iface_changes, routes_added, routes_removed, dns_added, dns_removed = _network_changes(diff)

    matched: list[str] = []
    findings: list[dict] = []
    remediation: list[str] = []

    # Critical service stopped on a production VM → fail
    is_prod = (environment or "").lower() in {"prod", "production"}
    critical_stopped = [svc for svc in services_removed if svc in CRITICAL_SERVICES_FOR_PRODUCTION]
    if critical_stopped and is_prod:
        for svc in critical_stopped:
            findings.append(
                {
                    "title": f"Critical service {svc!r} is no longer running",
                    "severity": "critical",
                    "confidence": "high",
                    "category": "service_loss",
                    "source_evidence": f"{svc} was active in the baseline snapshot",
                    "current_evidence": f"{svc} is absent from the post-migration service list",
                    "remediation": (
                        f"Investigate why {svc} failed to start. Common causes: "
                        "selinux booleans not migrated, service unit file missing, "
                        "or dependency (network/storage) not yet ready."
                    ),
                }
            )
            remediation.append(
                f"Restart {svc} via `systemctl start {svc.split('.')[0]}` and check its journal."
            )
        matched.append("tier2.critical_service_stopped")
        return TierClassification(
            tier="tier2",
            llm_required=False,
            rationale=(
                f"Detected {len(critical_stopped)} critical service(s) that "
                "stopped post-migration on a production VM. Returning a "
                "fail verdict without LLM analysis — the recovery action "
                "is unambiguous."
            ),
            verdict={
                "status": "fail",
                "summary": (
                    f"{len(critical_stopped)} critical production service(s) stopped: "
                    + ", ".join(sorted(critical_stopped))
                ),
                "findings": findings,
                "remediation": remediation,
                "confidence": "high",
            },
            matched_rules=matched,
        )

    # Critical service stopped on non-prod — flag as warn (still no LLM
    # needed; the recovery is the same, just lower urgency).
    if critical_stopped and not is_prod:
        for svc in critical_stopped:
            findings.append(
                {
                    "title": f"Service {svc!r} stopped (non-production)",
                    "severity": "warn",
                    "confidence": "high",
                    "category": "service_loss",
                    "source_evidence": f"{svc} was active in the baseline snapshot",
                    "current_evidence": f"{svc} is absent from the post-migration service list",
                    "remediation": (
                        f"Non-production scope — investigate at convenience. "
                        f"Restart via `systemctl start {svc.split('.')[0]}`."
                    ),
                }
            )
        matched.append("tier2.critical_service_stopped_nonprod")
        return TierClassification(
            tier="tier2",
            llm_required=False,
            rationale=(
                "Critical service(s) stopped on a non-production VM. "
                "Returning a warn verdict without LLM analysis."
            ),
            verdict={
                "status": "warn",
                "summary": (
                    f"{len(critical_stopped)} known service(s) stopped: "
                    + ", ".join(sorted(critical_stopped))
                ),
                "findings": findings,
                "remediation": remediation
                or [f"systemctl status {svc}" for svc in critical_stopped[:3]],
                "confidence": "high",
            },
            matched_rules=matched,
        )

    # All changes are known-good additions → pass without LLM
    unknown_added = [svc for svc in services_added if svc not in KNOWN_GOOD_NEW_SERVICES]
    only_known_good_services = services_added and not unknown_added and not services_removed
    no_other_changes = (
        not ports_removed
        and not ports_added
        and not mounts_removed
        and not mounts_added
        and not mounts_changed
        and not iface_changes
        and not routes_added
        and not routes_removed
        and not dns_added
        and not dns_removed
        and _is_empty_section(diff.get("cron"))
    )
    if only_known_good_services and no_other_changes:
        matched.append("tier2.only_known_good_services_added")
        return TierClassification(
            tier="tier2",
            llm_required=False,
            rationale=(
                "Only known-good services appeared (e.g. kubevirt-agent, "
                "qemu-guest-agent, cloud-init). No other diff entries. "
                "Returning pass without LLM analysis."
            ),
            verdict={
                "status": "pass",
                "summary": (
                    "Migration-tooling services added; no other drift. "
                    + "New services: "
                    + ", ".join(sorted(services_added))
                ),
                "findings": [],
                "remediation": [],
                "confidence": "high",
            },
            matched_rules=matched,
        )

    return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def classify(diff: dict, *, environment: str | None = None) -> TierClassification:
    """Decide which tier handles this diff.

    Args:
        diff: structured diff from :func:`LLMClient._diff_state`.
        environment: optional VM environment ("prod"/"production"/etc).
            Critical-service rules escalate to ``fail`` on production
            and ``warn`` everywhere else.

    Returns:
        :class:`TierClassification`. Tier-1 and Tier-2 results include
        a populated ``verdict``; Tier-3 leaves it ``None`` because the
        LLM call generates it.
    """
    t1 = _tier1(diff)
    if t1 is not None:
        logger.info("Validation classified as tier1: %s", t1.rationale)
        return t1
    t2 = _tier2(diff, environment=environment)
    if t2 is not None:
        logger.info("Validation classified as tier2 (%s): %s", t2.verdict["status"], t2.rationale)
        return t2
    logger.info("Validation classified as tier3 — invoking LLM")
    return TierClassification(
        tier="tier3",
        llm_required=True,
        rationale=(
            "Diff doesn't match Tier 1 (empty) or Tier 2 (known patterns). "
            "Routing to LLM for semantic analysis."
        ),
    )


# ---------------------------------------------------------------------------
# Tier preview — surfaces estimated distribution before running.
# Used by the bulk-validation preview endpoint.
# ---------------------------------------------------------------------------
def estimate_tier_distribution(
    diffs: Iterable[tuple[dict, str | None]],
) -> dict[str, int]:
    """Count classifications across a batch of (diff, environment) pairs.

    The bulk validation UI calls this with the precomputed diffs for
    each VM in scope so the operator sees, before submission:
    "Tier 1: 25, Tier 2: 10, Tier 3: 12 (estimated 12 LLM calls)".
    """
    counts = {"tier1": 0, "tier2": 0, "tier3": 0}
    for diff, env in diffs:
        result = classify(diff, environment=env)
        counts[result.tier] += 1
    return counts
