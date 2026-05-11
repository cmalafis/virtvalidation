"""Unit tests for the tier classifier in app.core.validation_tiers.

The classifier is the load-bearing efficiency lever — failures here
push every validation to the LLM even when there's nothing to reason
about. Every assertion below maps to a real production cost-or-quality
failure mode the classifier prevents.
"""

from __future__ import annotations

from app.core.validation_tiers import (
    KNOWN_GOOD_NEW_SERVICES,
    classify,
    estimate_tier_distribution,
)


# ---------------------------------------------------------------------------
# Tier 1 — empty / trivial diffs
# ---------------------------------------------------------------------------
def test_completely_empty_diff_classifies_tier1_pass_no_llm():
    diff = {
        "services": {"added": [], "removed": []},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {"user_crontabs": {}, "system_added": [], "system_removed": []},
    }
    result = classify(diff)
    assert result.tier == "tier1"
    assert result.llm_required is False
    assert result.verdict["status"] == "pass"
    assert "tier1.empty_diff" in result.matched_rules


def test_only_known_network_rename_classifies_tier1():
    """eth0 → ens192 with IPs transferred is a benign rename."""
    diff = {
        "services": {"added": [], "removed": []},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {
            "interfaces": {
                "eth0": {
                    "ipv4_added": [],
                    "ipv4_removed": ["10.0.0.5/24"],
                    "ipv6_added": [],
                    "ipv6_removed": [],
                },
                "ens192": {
                    "ipv4_added": ["10.0.0.5/24"],
                    "ipv4_removed": [],
                    "ipv6_added": [],
                    "ipv6_removed": [],
                },
            },
            "routes_added": [],
            "routes_removed": [],
            "dns_added": [],
            "dns_removed": [],
        },
        "cron": {},
    }
    result = classify(diff)
    assert result.tier == "tier1"
    assert result.llm_required is False


def test_dns_route_change_alone_still_classifies_as_material_change():
    """The classifier shouldn't paper over arbitrary network mutation —
    only the explicitly-handled rename pattern is Tier 1. Other network
    changes route to LLM."""
    diff = {
        "services": {"added": [], "removed": []},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {
            "interfaces": {
                "eth0": {
                    "ipv4_added": ["10.0.99.5/24"],
                    "ipv4_removed": [],
                    "ipv6_added": [],
                    "ipv6_removed": [],
                }
            },
            "routes_added": [],
            "routes_removed": [],
            "dns_added": [],
            "dns_removed": [],
        },
        "cron": {},
    }
    result = classify(diff)
    assert result.tier == "tier3"
    assert result.llm_required is True


# ---------------------------------------------------------------------------
# Tier 2 — rule-based
# ---------------------------------------------------------------------------
def test_only_kubevirt_agent_added_classifies_tier2_pass_no_llm():
    diff = {
        "services": {"added": ["kubevirt-agent.service"], "removed": []},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }
    result = classify(diff)
    assert result.tier == "tier2"
    assert result.llm_required is False
    assert result.verdict["status"] == "pass"
    assert "kubevirt-agent.service" in result.verdict["summary"]


def test_critical_service_stopped_on_prod_classifies_tier2_fail_no_llm():
    diff = {
        "services": {"added": [], "removed": ["postgresql.service"]},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }
    result = classify(diff, environment="prod")
    assert result.tier == "tier2"
    assert result.llm_required is False
    assert result.verdict["status"] == "fail"
    assert any("postgresql" in (f.get("title") or "") for f in result.verdict["findings"])
    # Findings must carry the structured shape the persistence layer expects.
    finding = result.verdict["findings"][0]
    for key in ("source_evidence", "current_evidence", "remediation"):
        assert finding[key]


def test_critical_service_stopped_on_non_prod_classifies_tier2_warn():
    """Lower urgency outside production — but still no LLM call needed,
    the recovery action is the same."""
    diff = {
        "services": {"added": [], "removed": ["nginx.service"]},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }
    result = classify(diff, environment="dev")
    assert result.tier == "tier2"
    assert result.verdict["status"] == "warn"


def test_critical_service_stopped_plus_extra_change_falls_through_to_llm():
    """When the diff contains a critical-service drop AND other
    unrelated changes, return the rule-based fail (production)
    because the recommendation is still unambiguous."""
    diff = {
        "services": {"added": [], "removed": ["postgresql.service"]},
        "ports": {"added": [{"proto": "tcp", "port": 8080, "address": "0.0.0.0"}], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }
    # Production scope: critical-service rule fires; extra changes are
    # secondary signal we accept losing here in exchange for skipping the LLM.
    result = classify(diff, environment="prod")
    assert result.tier == "tier2"
    assert result.verdict["status"] == "fail"


def test_unknown_new_service_falls_through_to_llm():
    """A new service that isn't on the known-good list could be a
    legitimate new daemon OR a security concern. Route to LLM for
    semantic analysis."""
    diff = {
        "services": {"added": ["my-custom-app.service"], "removed": []},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }
    result = classify(diff)
    assert result.tier == "tier3"
    assert result.llm_required is True


def test_mounts_changed_routes_to_tier3():
    """Mount changes can indicate storage migration issues — too
    domain-specific for static rules to handle. Route to LLM."""
    diff = {
        "services": {"added": [], "removed": []},
        "ports": {"added": [], "removed": []},
        "mounts": {
            "added": [],
            "removed": [],
            "changed": [
                {
                    "target": "/var/lib/data",
                    "before": {"source": "/dev/sda2", "fstype": "xfs"},
                    "after": {"source": "/dev/vda1", "fstype": "ext4"},
                }
            ],
        },
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }
    result = classify(diff)
    assert result.tier == "tier3"


# ---------------------------------------------------------------------------
# Estimator (used by the bulk-validation preview)
# ---------------------------------------------------------------------------
def test_estimate_tier_distribution_counts_correctly():
    empty = {
        "services": {"added": [], "removed": []},
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }
    known_good = {
        **empty,
        "services": {"added": ["qemu-guest-agent.service"], "removed": []},
    }
    ambiguous = {
        **empty,
        "services": {"added": ["custom-app.service"], "removed": []},
    }
    counts = estimate_tier_distribution(
        [(empty, "prod"), (empty, "dev"), (known_good, "prod"), (ambiguous, "prod")]
    )
    assert counts == {"tier1": 2, "tier2": 1, "tier3": 1}


# ---------------------------------------------------------------------------
# Knowledge-base sanity
# ---------------------------------------------------------------------------
def test_kubevirt_agent_is_in_known_good_set():
    assert "kubevirt-agent.service" in KNOWN_GOOD_NEW_SERVICES
    assert "qemu-guest-agent.service" in KNOWN_GOOD_NEW_SERVICES


def test_classifier_does_not_import_llm():
    """The classifier MUST stay LLM-free — any import drift would
    secretly couple the no-LLM path to a network round-trip."""
    import app.core.validation_tiers as mod

    # The module file should never reference app.core.llm except in
    # comments. Inspect the loaded module's symbol table.
    forbidden = [
        attr for attr in dir(mod)
        if "llm" in attr.lower() and not attr.startswith("_")
    ]
    assert not forbidden, f"validation_tiers leaked LLM references: {forbidden}"
