"""Tests for HA detection, spread strategy, and risk assessment.

Pins the contracts the Settings UI surfaces and the federal-customer
audit trail relies on:

  - HA-tagged VMs are detected from name patterns + sequential
    numeric suffixes.
  - ``ha_strategy="spread"`` splits an HA group into per-member
    micro-groups so the wave assigner naturally distributes them.
  - ``ha_strategy="together"`` keeps the original group cohesive.
  - Every group carries a RiskAssessment with factors + mitigations
    + rollback_complexity + estimated_downtime — the audit trail
    quotes these verbatim, so the catalog is part of the API
    contract.
"""

from __future__ import annotations

import pytest

from app.core.llm.mock_backend import MockBackend
from app.core.planner import MigrationPlanner
from app.core.preclassifier import (
    PreClassifier,
    detect_ha_members,
    detect_ha_role,
)
from app.models.vm import VM


def _vm(id_: int, name: str, **kwargs) -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.local",
        source_vcenter_id=kwargs.get("vcenter", 1),
        target_namespace=kwargs.get("target_namespace", "prod"),
        application_hint=kwargs.get("application_hint"),
        environment=kwargs.get("environment"),
        os_family=kwargs.get("os_family", "rhel"),
        role=kwargs.get("role", ""),
        vsphere_networks=list(kwargs.get("networks", [])),
        vsphere_datastores=list(kwargs.get("datastores", [])),
    )
    vm.id = id_
    return vm


# ---------------------------------------------------------------------------
# HA role detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected", [
    ("db-primary-01", "primary"),
    ("db-master", "primary"),
    ("postgres-leader", "primary"),
    ("db-replica-01", "replica"),
    ("db-secondary-01", "replica"),
    ("db-follower-2", "replica"),
    ("db-standby-01", "standby"),
    ("backup-passive", "standby"),
    ("misc-01", "standalone"),
])
def test_detect_ha_role_from_name_patterns(name, expected):
    assert detect_ha_role(name) == expected


def test_detect_ha_members_pattern_wins_over_sequence():
    vms = [
        _vm(1, "db-primary"),
        _vm(2, "db-replica-01"),
        _vm(3, "db-replica-02"),
    ]
    members = detect_ha_members(vms)
    by_id = {m.vm_id: m.ha_role for m in members}
    assert by_id == {1: "primary", 2: "replica", 3: "replica"}


def test_detect_ha_members_sequence_fallback():
    # No pattern hits — sequential cluster names. Lowest suffix wins
    # the "primary" tag; the rest become "member".
    vms = [
        _vm(1, "cassandra-node-01"),
        _vm(2, "cassandra-node-02"),
        _vm(3, "cassandra-node-03"),
    ]
    members = detect_ha_members(vms)
    by_id = {m.vm_id: m.ha_role for m in members}
    assert by_id[1] == "primary"
    assert by_id[2] == "member"
    assert by_id[3] == "member"


def test_detect_ha_members_single_vm_is_standalone():
    vms = [_vm(1, "alone-01")]
    members = detect_ha_members(vms)
    assert members[0].ha_role == "standalone"


def test_detect_ha_members_no_pattern_no_sequence_is_standalone():
    # Two VMs with non-numeric names — can't infer HA.
    vms = [_vm(1, "alpha"), _vm(2, "beta")]
    members = detect_ha_members(vms)
    assert all(m.ha_role == "standalone" for m in members)


# ---------------------------------------------------------------------------
# Group-level HA detection
# ---------------------------------------------------------------------------
def test_preclassifier_populates_ha_members_for_db_cluster():
    vms = [
        _vm(i, f"postgres-db-{i:02d}", application_hint="app1", networks=["db-net"])
        for i in range(1, 4)
    ]
    groups = PreClassifier().classify(vms)
    db_groups = [g for g in groups if g.estimated_role == "data"]
    assert db_groups
    for g in db_groups:
        assert g.ha_members, f"group {g.id} has no ha_members populated"
        # 3 sequentially-named VMs → 1 primary + 2 members.
        roles = sorted(m.ha_role for m in g.ha_members)
        assert roles == ["member", "member", "primary"]


def test_to_api_dict_exposes_ha_members():
    vms = [_vm(i, f"db-{i:02d}", application_hint="app1") for i in range(1, 4)]
    groups = PreClassifier().classify(vms)
    g = groups[0]
    body = g.to_api_dict()
    assert "ha_members" in body
    assert len(body["ha_members"]) == 3
    assert all("ha_role" in m for m in body["ha_members"])


# ---------------------------------------------------------------------------
# Spread strategy
# ---------------------------------------------------------------------------
def _ha_cluster_fleet() -> list[VM]:
    """Six VMs forming two HA clusters in the same vCenter."""
    return [
        _vm(1, "postgres-db-01", application_hint="app1", networks=["db-net"]),
        _vm(2, "postgres-db-02", application_hint="app1", networks=["db-net"]),
        _vm(3, "postgres-db-03", application_hint="app1", networks=["db-net"]),
        _vm(4, "ad-dc-01", application_hint="infra", networks=["infra-net"]),
        _vm(5, "ad-dc-02", application_hint="infra", networks=["infra-net"]),
        _vm(6, "ad-dc-03", application_hint="infra", networks=["infra-net"]),
    ]


def test_split_ha_group_produces_one_microgroup_per_member():
    vms = _ha_cluster_fleet()[:3]  # just the DB cluster
    groups = PreClassifier().classify(vms)
    assert len(groups) == 1
    micro = PreClassifier.split_ha_group(groups[0])
    assert len(micro) == 3
    # Each micro-group is exactly one VM.
    for g in micro:
        assert len(g.vm_ids) == 1
        assert len(g.ha_members) == 1


def test_split_ha_group_orders_primary_first():
    vms = _ha_cluster_fleet()[:3]
    groups = PreClassifier().classify(vms)
    micro = PreClassifier.split_ha_group(groups[0])
    # First micro-group should be the primary.
    assert micro[0].ha_members[0].ha_role == "primary"


def test_split_ha_group_no_ha_returns_original():
    vms = [_vm(1, "alone-only")]
    groups = PreClassifier().classify(vms)
    micro = PreClassifier.split_ha_group(groups[0])
    assert micro == [groups[0]]


def test_plan_with_groups_spread_default_distributes_ha_members():
    vms = _ha_cluster_fleet()
    planner = MigrationPlanner(backend=MockBackend())
    result = planner.plan_with_groups(vms)  # default: spread
    # The DB and DC clusters are HA; spread should generate ≥ 6
    # groups (3 DB members + 3 DC members at least).
    assert result["groups_formed"] >= 6
    # Every input VM placed exactly once.
    placed = sorted(vid for w in result["waves"] for vid in w["vm_ids"])
    assert placed == sorted(v.id for v in vms)


def test_plan_with_groups_together_keeps_ha_cohesive():
    vms = _ha_cluster_fleet()
    planner = MigrationPlanner(backend=MockBackend())
    result = planner.plan_with_groups(vms, ha_strategy="together")
    # Together: each HA cluster stays as one group → 2 groups total.
    assert result["groups_formed"] == 2


def test_plan_with_groups_auto_spreads_only_large_clusters():
    # 2-member cluster stays together under auto; 3-member spreads.
    vms = [
        _vm(1, "small-db-01", application_hint="small"),
        _vm(2, "small-db-02", application_hint="small"),
        _vm(3, "big-db-01", application_hint="big", networks=["big-net"]),
        _vm(4, "big-db-02", application_hint="big", networks=["big-net"]),
        _vm(5, "big-db-03", application_hint="big", networks=["big-net"]),
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(
        vms, ha_strategy="auto",
    )
    # small-db stays as 1 group; big-db splits into 3 micro-groups.
    # Total ≥ 4 (could be more if other splits happen, but at least
    # the big cluster contributes 3).
    assert result["groups_formed"] >= 4


# ---------------------------------------------------------------------------
# Risk assessment
# ---------------------------------------------------------------------------
def test_every_group_has_risk_assessment():
    vms = [
        _vm(i, f"web-{i:02d}", application_hint="app1", networks=["w"])
        for i in range(1, 4)
    ]
    groups = PreClassifier().classify(vms)
    for g in groups:
        assert g.risk_assessment is not None
        rd = g.risk_assessment.to_dict()
        assert rd["level"] in {"low", "medium", "high"}
        assert isinstance(rd["factors"], list)
        assert isinstance(rd["mitigations"], list)
        assert rd["rollback_complexity"] in {"low", "medium", "high"}
        assert rd["estimated_downtime"]


def test_stateful_data_group_carries_specific_factors():
    vms = [
        _vm(i, f"postgres-db-{i:02d}", application_hint="app1", networks=["db"])
        for i in range(1, 4)
    ]
    groups = PreClassifier().classify(vms)
    data_groups = [g for g in groups if g.estimated_role == "data"]
    assert data_groups
    g = data_groups[0]
    ra = g.risk_assessment
    assert ra.level == "high"
    text = " ".join(ra.factors).lower()
    assert "stateful" in text
    assert "database" in text or "data tier" in text
    # High-stakes data tier mitigations should mention backup + drain.
    mit = " ".join(ra.mitigations).lower()
    assert "backup" in mit
    assert "drain" in mit or "draining" in mit


def test_infrastructure_group_carries_secondary_failover_mitigation():
    vms = [
        _vm(i, f"ad-dc-{i:02d}", application_hint="infra", networks=["infra"])
        for i in range(1, 3)
    ]
    groups = PreClassifier().classify(vms)
    g = groups[0]
    assert g.estimated_role == "infrastructure"
    ra = g.risk_assessment
    mit = " ".join(ra.mitigations).lower()
    assert "ad" in mit or "secondary" in mit


def test_stateful_with_no_ha_peers_flags_single_point_of_failure():
    # Two VMs, no pattern hits, non-sequential names → no HA detected.
    vms = [
        _vm(1, "lone-postgres-alpha", application_hint="lone-app"),
    ]
    groups = PreClassifier().classify(vms)
    g = groups[0]
    factors = " ".join(g.risk_assessment.factors).lower()
    if g.estimated_state == "stateful":
        assert "single point of failure" in factors or "no ha" in factors


def test_large_group_carries_size_factor():
    # >10 VMs in a single group → "large group" factor.
    vms = [
        _vm(i, f"web-frontend-{i:02d}", application_hint="big-web", networks=["w"])
        for i in range(1, 13)
    ]
    groups = PreClassifier().classify(vms)
    # The web group has 12 VMs.
    web = [g for g in groups if g.estimated_role == "web"]
    assert web
    factors = " ".join(web[0].risk_assessment.factors).lower()
    assert "large group" in factors


def test_risk_assessment_in_plan_api_response():
    vms = [
        _vm(i, f"postgres-db-{i:02d}", application_hint="app1", networks=["d"])
        for i in range(1, 4)
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(
        vms, ha_strategy="together",
    )
    # The API surfaces risk_assessment on every group entry.
    for g in result["groups"]:
        assert "risk_assessment" in g
        ra = g["risk_assessment"]
        assert ra is not None
        assert ra["level"] in {"low", "medium", "high"}


# ---------------------------------------------------------------------------
# API integration
# ---------------------------------------------------------------------------
def test_post_plans_accepts_ha_strategy_field(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.llm_backend_type", "mock")
    from app.core.llm.factory import reset_backend_cache
    reset_backend_cache()
    # Seed three sequentially-named VMs.
    for i in range(1, 4):
        client.post("/api/vms", json={
            "name": f"postgres-db-{i:02d}",
            "source_hostname": f"db-{i}.local",
            "application_hint": "app1",
            "vsphere_networks": ["db-net"],
        }).raise_for_status()
    vm_ids = [r["id"] for r in client.get("/api/vms").json()["items"]]

    r_spread = client.post("/api/plans", json={
        "vm_ids": vm_ids,
        "ha_strategy": "spread",
    })
    assert r_spread.status_code == 201, r_spread.json()
    # Spread → 3 micro-groups for the 3-member cluster.
    assert r_spread.json()["groups_formed"] >= 3


def test_post_plans_rejects_invalid_ha_strategy(client):
    r = client.post("/api/plans", json={
        "vm_ids": [1],
        "ha_strategy": "chaos",
    })
    assert r.status_code == 422
