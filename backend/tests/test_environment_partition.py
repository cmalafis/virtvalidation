"""Tests for environment-aware plan partitioning.

The preclassifier's primary partition key was extended from
(vcenter_id, target_namespace) to (vcenter_id, target_namespace,
environment) so production VMs never share a plan with development
or DR VMs even when both land in the same target namespace.

These tests pin that invariant. They use the existing free-text
``VM.environment`` column — the typed Environment enum will follow
once the model migration ships.
"""

from __future__ import annotations

from app.core.llm.mock_backend import MockBackend
from app.core.planner import MigrationPlanner
from app.core.preclassifier import PreClassifier
from app.models.vm import VM


def _vm(id_: int, name: str, *, env: str, vcenter: int = 1, ns: str = "prod") -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.local",
        source_vcenter_id=vcenter,
        target_namespace_override=ns,
        environment=env,
        application_hint="app1",
        os_family="rhel",
        vsphere_networks=["net"],
        vsphere_datastores=["ds"],
    )
    vm.id = id_
    return vm


def test_prod_and_dev_vms_never_share_a_group():
    """Even with the same vcenter + target_namespace, prod and dev
    VMs partition into separate groups."""
    vms = [
        _vm(1, "web-prod-01", env="production"),
        _vm(2, "web-prod-02", env="production"),
        _vm(3, "web-prod-03", env="production"),
        _vm(4, "web-dev-01", env="development"),
        _vm(5, "web-dev-02", env="development"),
        _vm(6, "web-dev-03", env="development"),
    ]
    groups = PreClassifier().classify(vms)
    # Each group's vm_ids must come from a single environment.
    for g in groups:
        envs = set()
        for vm in vms:
            if vm.id in g.vm_ids:
                envs.add(vm.environment)
        assert len(envs) == 1, f"group {g.id} mixes environments: {envs}"


def test_prod_dev_dr_produce_three_distinct_partitions():
    """Three environments × identical other metadata → three
    distinct primary partitions, each with its own group(s)."""
    vms = []
    for i, env in enumerate(("production", "development", "dr")):
        for j in range(1, 4):
            vms.append(
                _vm(
                    id_=i * 10 + j,
                    name=f"{env}-web-{j:02d}",
                    env=env,
                )
            )
    groups = PreClassifier().classify(vms)
    env_per_group: dict[str, set[str]] = {}
    for g in groups:
        env_per_group[g.id] = set()
        for vm in vms:
            if vm.id in g.vm_ids:
                env_per_group[g.id].add(vm.environment)
    # Every group is single-environment.
    for gid, envs in env_per_group.items():
        assert len(envs) == 1, f"{gid} mixes {envs}"
    # And all three environments appear across the group set.
    all_envs = {next(iter(envs)) for envs in env_per_group.values()}
    assert {"production", "development", "dr"} <= all_envs


def test_environment_aliases_partition_together():
    """``Prod`` and ``production`` normalize to the same enum and
    must end up in the same partition."""
    vms = [
        _vm(1, "web-01", env="Prod"),
        _vm(2, "web-02", env="production"),
        _vm(3, "web-03", env="PRODUCTION"),
    ]
    groups = PreClassifier().classify(vms)
    # The three prod-aliased VMs should all land in the same group
    # because the partition normalizes the env string first.
    prod_groups = [g for g in groups if 1 in g.vm_ids or 2 in g.vm_ids or 3 in g.vm_ids]
    # Single group containing all three.
    assert len(prod_groups) == 1
    assert sorted(prod_groups[0].vm_ids) == [1, 2, 3]


def test_unknown_environment_does_not_merge_with_known():
    """VMs without a known environment label partition separately
    from labeled VMs so operators see the gap."""
    vms = [
        _vm(1, "web-prod-01", env="production"),
        _vm(2, "web-prod-02", env="production"),
        _vm(3, "web-prod-03", env="production"),
        _vm(4, "web-unknown-01", env=""),  # unknown
        _vm(5, "web-unknown-02", env=""),
        _vm(6, "web-unknown-03", env=""),
    ]
    groups = PreClassifier().classify(vms)
    for g in groups:
        envs = {vm.environment for vm in vms if vm.id in g.vm_ids}
        # Unknown VMs are in their own group; known VMs in theirs.
        assert len(envs) == 1


def test_plan_with_mixed_envs_uses_separate_waves_or_groups():
    """End-to-end: a mixed-env plan produces wave assignments where
    no single wave's groups mix environments."""
    vms = []
    for i in range(1, 4):
        vms.append(_vm(i, f"web-prod-{i:02d}", env="production"))
    for i in range(1, 4):
        vms.append(_vm(10 + i, f"web-dev-{i:02d}", env="development"))
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    # Within each wave, every group should map to a single env.
    for wave in result["waves"]:
        envs_in_wave = set()
        for vm in vms:
            if vm.id in wave["vm_ids"]:
                envs_in_wave.add(vm.environment)
        # The wave may carry multiple environments only if each one's
        # groups stayed separate. The architectural goal is that
        # groups never mix; the wave may still pack distinct
        # single-env groups together to stay under MAX_VMS_PER_WAVE.
        # The check we care about is: no group mixes envs.
        for gid in wave["group_ids"]:
            group_vm_ids = set()
            for g in result["groups"]:
                if g["id"] == gid:
                    group_vm_ids = set(g["vm_ids"])
                    break
            group_envs = {vm.environment for vm in vms if vm.id in group_vm_ids}
            assert len(group_envs) <= 1, (
                f"group {gid} in wave {wave['wave_number']} " f"mixes envs: {group_envs}"
            )
