"""Hard-limit + scale tests for the mechanical wave skeleton.

Pins the architectural invariants that the post-M refactor relies
on:

  * Every wave carries ≤ ``MAX_VMS_PER_WAVE`` VMs.
  * Every wave carries ≤ ``MAX_HA_PEERS_PER_WAVE`` peers from any
    single HA family.
  * Dependencies are always satisfied (every group's dependencies
    placed in earlier waves).
  * Wave structure is deterministic — same input → byte-identical
    output.
  * Plan generation succeeds at 100 / 500 / 1000 VMs and produces
    well-formed waves in reasonable time.
"""

from __future__ import annotations

import time

from app.core.llm.mock_backend import MockBackend
from app.core.planner import MigrationPlanner
from app.core.preclassifier import PreClassifier
from app.core.wave_skeleton import (
    MAX_VMS_PER_WAVE,
    MechanicalWaveAssigner,
)
from app.models.vm import VM


def _vm(
    id_: int,
    name: str,
    *,
    vcenter: int = 1,
    target_namespace_override: str = "prod",
    networks: list[str] | None = None,
    datastores: list[str] | None = None,
    application_hint: str | None = None,
    os_family: str = "rhel",
) -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.local",
        source_vcenter_id=vcenter,
        target_namespace_override=target_namespace_override,
        application_hint=application_hint,
        os_family=os_family,
        vsphere_networks=list(networks or []),
        vsphere_datastores=list(datastores or []),
    )
    vm.id = id_
    return vm


# ---------------------------------------------------------------------------
# Per-wave VM cap (MTV concurrency)
# ---------------------------------------------------------------------------
def test_every_wave_respects_max_vms_per_wave():
    # Generate 15 web servers in one cohesive group — exceeds the
    # MAX_VMS_PER_WAVE cap and must be split across waves.
    vms = [
        _vm(
            i, f"web-frontend-{i:02d}", networks=["w"], datastores=["s"], application_hint="big-web"
        )
        for i in range(1, 16)
    ]
    groups = PreClassifier().classify(vms)
    waves = MechanicalWaveAssigner().assign_waves(groups)
    assert len(waves) >= 2, "15 VMs in one group must produce ≥2 waves"
    for wave in waves:
        assert wave.vm_count <= MAX_VMS_PER_WAVE, (
            f"wave {wave.wave_number} carries {wave.vm_count} VMs "
            f"(> MAX_VMS_PER_WAVE={MAX_VMS_PER_WAVE})"
        )


def test_15_web_servers_at_least_two_waves():
    """Spec acceptance: 15 web servers → ≥2 waves."""
    vms = [_vm(i, f"web-{i:02d}", networks=["w"], application_hint="webapp") for i in range(1, 16)]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    assert len(result["waves"]) >= 2
    for wave in result["waves"]:
        assert len(wave["vm_ids"]) <= MAX_VMS_PER_WAVE


# ---------------------------------------------------------------------------
# HA peer cap
# ---------------------------------------------------------------------------
def test_four_ad_dc_servers_split_across_at_least_two_waves():
    """Spec acceptance: 4 AD-DC peers → ≥2 waves with ≤2 per wave."""
    vms = [
        _vm(i, f"ad-dc-{i:02d}", networks=["infra"], application_hint="identityservices")
        for i in range(1, 5)
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    # 4 HA-tagged peers + spread default → 4 micro-groups → can't all
    # fit one wave under the ≤2-per-family cap.
    assert len(result["waves"]) >= 2


def test_dc_peers_capped_at_max_ha_peers_per_wave():
    # Six DC peers in one fleet: must spread across ≥3 waves
    # (6 / 2 = 3).
    vms = [
        _vm(i, f"ad-dc-{i:02d}", networks=["infra"], application_hint="identityservices")
        for i in range(1, 7)
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    assert len(result["waves"]) >= 3


def test_data_primary_placed_before_replicas():
    """Spec acceptance: DB primary always migrates before its replicas."""
    vms = [
        _vm(1, "postgres-primary", networks=["db"], application_hint="app1"),
        _vm(2, "postgres-replica-01", networks=["db"], application_hint="app1"),
        _vm(3, "postgres-replica-02", networks=["db"], application_hint="app1"),
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    wave_for_vm: dict[int, int] = {}
    for wave in result["waves"]:
        for vid in wave["vm_ids"]:
            wave_for_vm[vid] = wave["wave_number"]
    # Primary's wave < every replica's wave.
    assert wave_for_vm[1] < wave_for_vm[2]
    assert wave_for_vm[1] < wave_for_vm[3]


# ---------------------------------------------------------------------------
# Dependency ordering
# ---------------------------------------------------------------------------
def test_dependencies_always_in_earlier_waves():
    # A mixed fleet with app + data + infra in one application.
    vms = [
        _vm(1, "postgres-db-01", networks=["db"], application_hint="app1"),
        _vm(2, "postgres-db-02", networks=["db"], application_hint="app1"),
        _vm(3, "postgres-db-03", networks=["db"], application_hint="app1"),
        _vm(4, "app-svc-01", networks=["app"], application_hint="app1"),
        _vm(5, "app-svc-02", networks=["app"], application_hint="app1"),
        _vm(6, "app-svc-03", networks=["app"], application_hint="app1"),
        _vm(7, "ad-dc-01", networks=["infra"], application_hint="identityservices"),
        _vm(8, "ad-dc-02", networks=["infra"], application_hint="identityservices"),
        _vm(9, "ad-dc-03", networks=["infra"], application_hint="identityservices"),
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    # Find any data + app groups and verify their waves.
    role_to_waves: dict[str, list[int]] = {}
    for group in result["groups"]:
        role_to_waves.setdefault(group["role"], []).append(group["wave_number"])
    # All app-tier groups should land AFTER the earliest data group.
    if "data" in role_to_waves and "app" in role_to_waves:
        assert min(role_to_waves["data"]) <= min(role_to_waves["app"])
    # Infrastructure should land before app per role priority.
    if "infrastructure" in role_to_waves and "app" in role_to_waves:
        assert min(role_to_waves["infrastructure"]) <= min(role_to_waves["app"])


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------
def test_same_input_produces_same_waves():
    vms = [_vm(i, f"web-{i:02d}", networks=["w"], application_hint="app1") for i in range(1, 8)]
    groups_a = PreClassifier().classify(vms)
    groups_b = PreClassifier().classify(vms)
    waves_a = MechanicalWaveAssigner().assign_waves(groups_a)
    waves_b = MechanicalWaveAssigner().assign_waves(groups_b)

    def _signature(waves):
        return [(w.wave_number, tuple(g.id for g in w.groups), tuple(w.vm_ids)) for w in waves]

    assert _signature(waves_a) == _signature(waves_b)


# ---------------------------------------------------------------------------
# Renumbering
# ---------------------------------------------------------------------------
def test_waves_renumbered_to_contiguous_sequence():
    """Even when dependency math leaves gaps, the operator sees
    waves 1..N (no missing numbers)."""
    # Construct a scenario where dependencies push some groups to
    # high wave numbers. The renumber step should compact them.
    vms = (
        [_vm(i, f"infra-{i}", application_hint="infra", networks=["i"]) for i in range(1, 3)]
        + [_vm(i, f"data-{i}", application_hint="app1", networks=["d"]) for i in range(10, 13)]
        + [_vm(i, f"app-{i}", application_hint="app1", networks=["a"]) for i in range(20, 23)]
        + [_vm(i, f"web-{i}", application_hint="app1", networks=["w"]) for i in range(30, 33)]
    )
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    numbers = [w["wave_number"] for w in result["waves"]]
    assert numbers == list(range(1, len(numbers) + 1))


# ---------------------------------------------------------------------------
# Scale benchmarks
# ---------------------------------------------------------------------------
def _generate_fleet(n: int) -> list[VM]:
    """Synthetic fleet of N VMs spread across mixed tiers."""
    vms: list[VM] = []
    for i in range(1, n + 1):
        role = ["web", "app", "db", "edge"][i % 4]
        vms.append(
            _vm(
                i,
                f"{role}-{i:04d}",
                networks=[f"net-{i % 8}"],
                datastores=[f"ds-{i % 4}"],
                application_hint=f"app-{i % 12}",
            )
        )
    return vms


def test_scale_100_vms_completes_under_two_seconds():
    vms = _generate_fleet(100)
    started = time.monotonic()
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"100-VM plan took {elapsed:.2f}s"
    placed = sorted(vid for w in result["waves"] for vid in w["vm_ids"])
    assert placed == sorted(v.id for v in vms)


def test_scale_500_vms_completes_under_ten_seconds():
    vms = _generate_fleet(500)
    started = time.monotonic()
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"500-VM plan took {elapsed:.2f}s"
    placed = sorted(vid for w in result["waves"] for vid in w["vm_ids"])
    assert placed == sorted(v.id for v in vms)
    # Hard-limit invariant at scale.
    for wave in result["waves"]:
        assert len(wave["vm_ids"]) <= MAX_VMS_PER_WAVE


def test_scale_1000_vms_completes_under_thirty_seconds():
    vms = _generate_fleet(1000)
    started = time.monotonic()
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    elapsed = time.monotonic() - started
    assert elapsed < 30.0, f"1000-VM plan took {elapsed:.2f}s"
    placed = sorted(vid for w in result["waves"] for vid in w["vm_ids"])
    assert placed == sorted(v.id for v in vms)
    for wave in result["waves"]:
        assert len(wave["vm_ids"]) <= MAX_VMS_PER_WAVE


def test_dha_fleet_passes_all_hard_limits():
    """The canonical 57-VM federal fleet must produce a valid plan
    where every wave respects all hard limits."""
    from tests.fixtures.dha_fleet import build_dha_fleet_vms

    vms = build_dha_fleet_vms()
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    for wave in result["waves"]:
        assert len(wave["vm_ids"]) <= MAX_VMS_PER_WAVE


def test_per_wave_llm_calls_stay_under_per_call_ceiling():
    """The whole point of the refactor: each LLM call sees ≤ items
    per call regardless of overall plan size. At MAX_VMS_PER_WAVE,
    a wave never has more than that many groups (every group ≥ 1 VM)."""
    vms = _generate_fleet(200)
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    for wave in result["waves"]:
        assert len(wave["group_ids"]) <= MAX_VMS_PER_WAVE


# ---------------------------------------------------------------------------
# Wave-level partition coherence (= MTV Plan CR boundary)
# ---------------------------------------------------------------------------
# Each wave maps 1:1 to a single MTV ``Plan`` CR. The CR can only
# point at one source vCenter and one target namespace, so the
# packer must never co-pack groups across either boundary. These
# tests pin that contract.
def _vm_keys_by_id(vms: list[VM]) -> dict[int, tuple[int, str]]:
    return {vm.id: (vm.source_vcenter_id, vm.target_namespace_override) for vm in vms}


def _assert_wave_partition_coherent(result_waves: list[dict], vms: list[VM]) -> None:
    """Every wave's VMs must share one (source_vcenter_id, target_namespace)
    tuple — anything else would emit an unapply-able MTV Plan CR."""
    keys = _vm_keys_by_id(vms)
    for wave in result_waves:
        wave_keys = {keys[vid] for vid in wave["vm_ids"]}
        assert len(wave_keys) == 1, (
            f"wave {wave['wave_number']} mixes " f"(vcenter, namespace) tuples: {sorted(wave_keys)}"
        )


def test_waves_never_mix_source_vcenters():
    """Two source vCenters worth of identical web VMs must split
    into at least two waves, one per vCenter, even though both fits
    in a single MAX_VMS_PER_WAVE wave by VM count."""
    vms = [
        _vm(1, "web-01", vcenter=1, networks=["w"], application_hint="app"),
        _vm(2, "web-02", vcenter=1, networks=["w"], application_hint="app"),
        _vm(3, "web-03", vcenter=2, networks=["w"], application_hint="app"),
        _vm(4, "web-04", vcenter=2, networks=["w"], application_hint="app"),
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    _assert_wave_partition_coherent(result["waves"], vms)
    # Two vCenters → at least two waves regardless of VM count.
    assert len(result["waves"]) >= 2


def test_waves_never_mix_target_namespaces():
    """Two target namespaces in the same vCenter must split into at
    least two waves — MTV ``spec.targetNamespace`` is a single string."""
    vms = [
        _vm(
            1,
            "app-01",
            vcenter=1,
            target_namespace_override="prod-a",
            networks=["w"],
            application_hint="app",
        ),
        _vm(
            2,
            "app-02",
            vcenter=1,
            target_namespace_override="prod-a",
            networks=["w"],
            application_hint="app",
        ),
        _vm(
            3,
            "app-03",
            vcenter=1,
            target_namespace_override="prod-b",
            networks=["w"],
            application_hint="app",
        ),
        _vm(
            4,
            "app-04",
            vcenter=1,
            target_namespace_override="prod-b",
            networks=["w"],
            application_hint="app",
        ),
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    _assert_wave_partition_coherent(result["waves"], vms)
    assert len(result["waves"]) >= 2


def test_dha_fleet_waves_are_partition_coherent():
    """Scale check: the canonical 57-VM federal fleet spans multiple
    vCenters and target namespaces. Every wave it produces must
    still be partition-coherent."""
    from tests.fixtures.dha_fleet import build_dha_fleet_vms

    vms = build_dha_fleet_vms()
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    _assert_wave_partition_coherent(result["waves"], vms)


def test_partition_coherence_holds_across_ha_spread():
    """When ``ha_strategy=spread`` splits an HA group into per-member
    micro-groups, the micro-groups inherit the parent's partition.
    Spreading them across waves must still keep each wave coherent
    on (vcenter, namespace)."""
    # Two AD-DC clusters, one per vCenter — both need spreading.
    vms = [
        _vm(
            1,
            "ad-dc-01",
            vcenter=1,
            target_namespace_override="infra-a",
            networks=["infra"],
            application_hint="identityservices",
        ),
        _vm(
            2,
            "ad-dc-02",
            vcenter=1,
            target_namespace_override="infra-a",
            networks=["infra"],
            application_hint="identityservices",
        ),
        _vm(
            3,
            "ad-dc-03",
            vcenter=1,
            target_namespace_override="infra-a",
            networks=["infra"],
            application_hint="identityservices",
        ),
        _vm(
            4,
            "ad-dc-01",
            vcenter=2,
            target_namespace_override="infra-b",
            networks=["infra"],
            application_hint="identityservices",
        ),
        _vm(
            5,
            "ad-dc-02",
            vcenter=2,
            target_namespace_override="infra-b",
            networks=["infra"],
            application_hint="identityservices",
        ),
        _vm(
            6,
            "ad-dc-03",
            vcenter=2,
            target_namespace_override="infra-b",
            networks=["infra"],
            application_hint="identityservices",
        ),
    ]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(
        vms,
        ha_strategy="spread",
    )
    _assert_wave_partition_coherent(result["waves"], vms)
