"""Tests for the mechanical pre-classifier.

Pins the determinism + group cardinality + role-detection contracts
the planner downstream depends on. The planner's wave assignment is
correct ONLY because pre-classification reliably keeps the LLM input
small and consistent — these tests are the load-bearing fixture for
that invariant.
"""

from __future__ import annotations

import time

import pytest

from app.core.preclassifier import (
    PreClassifier,
    detect_risk,
    detect_role,
    detect_state,
)
from app.models.vm import VM


def _vm(
    id_: int,
    name: str,
    *,
    vcenter: int | None = 1,
    target_namespace: str = "prod",
    networks: list[str] | None = None,
    datastores: list[str] | None = None,
    application_hint: str | None = None,
    environment: str | None = None,
    role: str = "",
    os_family: str = "rhel",
) -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.local",
        source_vcenter_id=vcenter,
        target_namespace=target_namespace,
        vsphere_networks=list(networks or []),
        vsphere_datastores=list(datastores or []),
        application_hint=application_hint,
        environment=environment,
        role=role,
        os_family=os_family,
    )
    vm.id = id_
    return vm


# ---------------------------------------------------------------------------
# Role / state / risk detection
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name,expected_role", [
    ("web-prod-01", "web"),
    ("nginx-edge-1", "edge"),
    ("postgres-db-prod", "data"),
    ("mongo-replica-2", "data"),
    ("app-api-svc-3", "app"),
    ("ldap-dc-01", "infrastructure"),
    ("haproxy-front", "edge"),
    ("payroll", "other"),
    ("oracle-db-01", "data"),
])
def test_detect_role_from_name_patterns(name, expected_role):
    vm = _vm(1, name)
    assert detect_role(vm) == expected_role


def test_detect_role_respects_operator_hint():
    vm = _vm(1, "payroll", role="data")
    assert detect_role(vm) == "data"


def test_detect_state_pins_stateful_roles():
    vm = _vm(1, "db-01")
    assert detect_state(vm, "data") == "stateful"
    assert detect_state(vm, "infrastructure") == "stateful"


def test_detect_state_pins_stateless_roles():
    vm = _vm(1, "web-01")
    assert detect_state(vm, "web") == "stateless"
    assert detect_state(vm, "app") == "stateless"
    assert detect_state(vm, "edge") == "stateless"


def test_detect_state_unknown_for_other():
    vm = _vm(1, "misc-01")
    assert detect_state(vm, "other") == "unknown"


def test_detect_risk_stateful_data_is_high():
    assert detect_risk("data", "stateful", 1, False) == "high"
    assert detect_risk("infrastructure", "stateful", 1, False) == "high"


def test_detect_risk_stateless_web_is_low_or_medium():
    assert detect_risk("web", "stateless", 2, False) == "low"
    assert detect_risk("web", "stateless", 10, False) == "medium"


def test_detect_risk_sequential_pair_is_medium():
    assert detect_risk("other", "unknown", 2, True) == "medium"


# ---------------------------------------------------------------------------
# Classification — single-bucket cases
# ---------------------------------------------------------------------------
def test_five_similar_vms_collapse_to_one_group():
    vms = [
        _vm(i, f"web-prod-{i:02d}", networks=["prod-web"], datastores=["nfs-prod"])
        for i in range(1, 6)
    ]
    groups = PreClassifier().classify(vms)
    # Same primary key + shared net + shared ds + shared name prefix.
    assert len(groups) == 1
    assert sorted(groups[0].vm_ids) == [1, 2, 3, 4, 5]
    assert groups[0].estimated_role == "web"
    assert groups[0].estimated_state == "stateless"


def test_application_hint_partitions_with_role_subgroups():
    # Six epic-emr VMs (3 web + 3 db) so the small-primary collapse
    # (<5 VMs threshold) doesn't merge web + db into one group.
    vms = [
        _vm(1, "web-01", application_hint="epic-emr", networks=["a"]),
        _vm(2, "web-02", application_hint="epic-emr", networks=["a"]),
        _vm(3, "web-03", application_hint="epic-emr", networks=["a"]),
        _vm(4, "db-01",  application_hint="epic-emr", networks=["b"]),
        _vm(5, "db-02",  application_hint="epic-emr", networks=["b"]),
        _vm(6, "db-03",  application_hint="epic-emr", networks=["b"]),
        _vm(7, "loose-01", target_namespace="staging-only"),
    ]
    groups = PreClassifier().classify(vms)
    # Operator hint scopes the partition; within the hint we sub-
    # split by role so wave ordering can stage db → web within
    # epic-emr. Both subgroups are still tagged as the same
    # application.
    hint_groups = [g for g in groups if "epic-emr" in g.id]
    assert len(hint_groups) == 2
    members = {vm_id for g in hint_groups for vm_id in g.vm_ids}
    assert members == {1, 2, 3, 4, 5, 6}
    # loose-01 falls into its own primary partition (different
    # target_namespace) and gets a name-prefix bucket there.
    assert any(7 in g.vm_ids for g in groups if "epic-emr" not in g.id)


def test_cross_vcenter_vms_never_merge():
    vms = [
        _vm(1, "web-01", vcenter=1),
        _vm(2, "web-01-b", vcenter=2),
    ]
    groups = PreClassifier().classify(vms)
    # Two primary partitions ⇒ at least two groups.
    vc_ids = {g.key.vcenter_id for g in groups}
    assert vc_ids == {1, 2}
    assert len(groups) >= 2


def test_cross_target_namespace_vms_never_merge():
    vms = [
        _vm(1, "web-01", target_namespace="prod"),
        _vm(2, "web-02", target_namespace="staging"),
    ]
    groups = PreClassifier().classify(vms)
    ns = {g.key.target_namespace for g in groups}
    assert ns == {"prod", "staging"}


def test_data_vms_get_stateful_and_high_risk():
    vms = [
        _vm(i, f"postgres-db-{i:02d}", networks=["db-net"], datastores=["nfs-db"])
        for i in range(1, 4)
    ]
    groups = PreClassifier().classify(vms)
    assert len(groups) == 1
    g = groups[0]
    assert g.estimated_role == "data"
    assert g.estimated_state == "stateful"
    assert g.migration_risk == "high"


def test_web_singleton_gets_low_risk():
    vms = [_vm(1, "web-prod-01", networks=["prod-web"])]
    groups = PreClassifier().classify(vms)
    assert len(groups) == 1
    assert groups[0].estimated_role == "web"
    assert groups[0].migration_risk == "low"


# ---------------------------------------------------------------------------
# Realistic 57-VM RVTools fleet
# ---------------------------------------------------------------------------
def _realistic_fleet() -> list[VM]:
    """Mirror the RVTools fixture the planner test uses: 57 VMs split
    across two vCenters, four app hints, mix of web/app/db tiers."""
    vms: list[VM] = []
    idx = 1
    # vCenter 1 — epic-emr application
    for i in range(1, 16):
        vms.append(_vm(idx, f"epic-web-{i:02d}", networks=["epic-web-net"], datastores=["nfs-epic"], application_hint="epic-emr"))
        idx += 1
    for i in range(1, 11):
        vms.append(_vm(idx, f"epic-app-{i:02d}", networks=["epic-app-net"], datastores=["nfs-epic"], application_hint="epic-emr"))
        idx += 1
    for i in range(1, 5):
        vms.append(_vm(idx, f"epic-db-{i:02d}", networks=["epic-db-net"], datastores=["ssd-epic"], application_hint="epic-emr"))
        idx += 1
    # vCenter 1 — payroll application
    for i in range(1, 7):
        vms.append(_vm(idx, f"payroll-app-{i:02d}", networks=["payroll-net"], datastores=["nfs-payroll"], application_hint="payroll"))
        idx += 1
    # vCenter 2 — analytics
    for i in range(1, 13):
        vms.append(_vm(idx, f"analytics-worker-{i:02d}", vcenter=2, networks=["analytics-net"], datastores=["object-analytics"], application_hint="analytics"))
        idx += 1
    # vCenter 2 — infrastructure
    for i in range(1, 6):
        vms.append(_vm(idx, f"dc-ldap-{i:02d}", vcenter=2, networks=["infra-net"], datastores=["ssd-infra"], application_hint="infra"))
        idx += 1
    return vms


def test_realistic_50_vm_fleet_produces_reasonable_group_count():
    vms = _realistic_fleet()
    fleet_size = len(vms)
    # The fixture is sized to approximate the production-customer 57-VM
    # RVTools export — exact count varies as we tune the fixture, but
    # the invariant we care about is "small group count regardless".
    assert 50 <= fleet_size <= 60, f"fleet size unexpected: {fleet_size}"
    groups = PreClassifier().classify(vms)
    # 5 application_hints × up to 3 roles ≈ 6-12 groups expected, well
    # under the LLM ceiling of 20.
    assert 5 <= len(groups) <= 15, f"got {len(groups)} groups: {[g.id for g in groups]}"
    placed = sum(len(g.vm_ids) for g in groups)
    assert placed == fleet_size


def test_realistic_fleet_dependency_hints_seeded():
    vms = _realistic_fleet()
    groups = PreClassifier().classify(vms)
    app_groups = [g for g in groups if g.estimated_role == "app"]
    assert app_groups, "expected at least one app-tier group"
    # App groups should hint at data + infrastructure groups.
    for g in app_groups:
        assert g.dependency_hints, f"app group {g.id} lost dependency hints"


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_same_input_produces_same_output():
    vms = _realistic_fleet()
    first = [g.to_api_dict() for g in PreClassifier().classify(vms)]
    second = [g.to_api_dict() for g in PreClassifier().classify(vms)]
    assert first == second


def test_shuffling_input_does_not_change_output_groups():
    import random
    vms = _realistic_fleet()
    rng = random.Random(42)
    shuffled = list(vms)
    rng.shuffle(shuffled)
    # Compare group IDs + member sets — the ORDER within shared
    # attributes might shift if we used a non-stable iteration, so the
    # invariant we care about is "same VMs land in same groups".
    by_id_a = {g.id: set(g.vm_ids) for g in PreClassifier().classify(vms)}
    by_id_b = {g.id: set(g.vm_ids) for g in PreClassifier().classify(shuffled)}
    assert by_id_a == by_id_b


# ---------------------------------------------------------------------------
# Performance + cardinality ceiling
# ---------------------------------------------------------------------------
def test_classify_1000_vms_under_5_seconds():
    vms: list[VM] = []
    for i in range(1, 1001):
        role_token = ["web", "app", "db", "edge"][i % 4]
        vms.append(_vm(
            i, f"{role_token}-prod-{i:04d}",
            networks=[f"net-{i % 8}"],
            datastores=[f"ds-{i % 4}"],
            application_hint=f"app-{i % 25}",
        ))
    started = time.monotonic()
    groups = PreClassifier().classify(vms)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"1000-VM classify took {elapsed:.2f}s; expected <5s"
    assert sum(len(g.vm_ids) for g in groups) == 1000


def test_consolidation_caps_group_count_at_max_groups():
    # Force lots of distinct app_hints so the classifier produces more
    # raw groups than the cap allows, then verify consolidation.
    vms = []
    for i in range(1, 51):
        vms.append(_vm(
            i, f"vm-{i:02d}",
            application_hint=f"unique-{i}",
            networks=[f"net-{i}"],
        ))
    classifier = PreClassifier(max_groups=8)
    groups = classifier.classify(vms)
    assert len(groups) <= 8
    # No VM lost during consolidation.
    placed = sum(len(g.vm_ids) for g in groups)
    assert placed == 50


def test_consolidation_never_merges_across_primary_key():
    vms = [
        _vm(i, f"vc1-vm-{i}", vcenter=1, application_hint=f"a-{i}")
        for i in range(1, 11)
    ] + [
        _vm(20 + i, f"vc2-vm-{i}", vcenter=2, application_hint=f"b-{i}")
        for i in range(1, 11)
    ]
    classifier = PreClassifier(max_groups=4)
    groups = classifier.classify(vms)
    # Even under tight cap, the vCenter partition is non-negotiable.
    vc_ids = {g.key.vcenter_id for g in groups}
    assert vc_ids == {1, 2}


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------
def test_classify_empty_returns_empty():
    assert PreClassifier().classify([]) == []
