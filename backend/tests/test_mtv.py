"""Tests for app.core.mtv — wave-to-MTV-YAML conversion."""

from __future__ import annotations

import pytest
import yaml

from app.core.mtv import MappingResolver, MTVGenerationError, WaveContext, generate_wave_yaml


@pytest.fixture
def ctx() -> WaveContext:
    return WaveContext(
        plan_id=42,
        wave_number=2,
        rationale="DB tier — shared DB Backend portgroup, single fast NFS datastore",
        namespace="openshift-mtv",
        source_provider="vmware-prod",
        destination_provider="ocpv-host",
        default_target_namespace="finance-prod",
    )


@pytest.fixture
def two_vms_one_network_one_datastore() -> list[dict]:
    return [
        {
            "name": "db-prod-01",
            "vsphere_networks": ["DB Backend"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "db-backend-nad",
        },
        {
            "name": "db-prod-02",
            "vsphere_networks": ["DB Backend"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "db-backend-nad",
        },
    ]


def _docs(yaml_text: str) -> list[dict]:
    return list(yaml.safe_load_all(yaml_text))


def test_generates_three_resources_in_order(ctx, two_vms_one_network_one_datastore):
    docs = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))
    assert [d["kind"] for d in docs] == ["NetworkMap", "StorageMap", "Plan"]
    for d in docs:
        assert d["apiVersion"] == "forklift.konveyor.io/v1beta1"
        assert d["metadata"]["namespace"] == "openshift-mtv"


def test_resource_names_are_derived_from_plan_and_wave(ctx, two_vms_one_network_one_datastore):
    docs = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))
    netmap, storagemap, plan = docs
    assert netmap["metadata"]["name"] == "vv-plan-42-wave-2-netmap"
    assert storagemap["metadata"]["name"] == "vv-plan-42-wave-2-storagemap"
    assert plan["metadata"]["name"] == "vv-plan-42-wave-2"


def test_network_map_dedupes_shared_portgroup(ctx, two_vms_one_network_one_datastore):
    netmap = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))[0]
    entries = netmap["spec"]["map"]
    assert len(entries) == 1
    assert entries[0]["source"]["name"] == "DB Backend"
    assert entries[0]["destination"]["type"] == "multus"
    assert entries[0]["destination"]["name"] == "db-backend-nad"
    assert entries[0]["destination"]["namespace"] == "finance-prod"


def test_storage_map_dedupes_shared_datastore(ctx, two_vms_one_network_one_datastore):
    storagemap = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))[1]
    entries = storagemap["spec"]["map"]
    assert len(entries) == 1
    assert entries[0]["source"]["name"] == "nfs-prod-fast"
    assert entries[0]["destination"]["storageClass"] == "ocs-storagecluster-cephfs"


def test_multiple_distinct_networks_emit_distinct_entries(ctx):
    vms = [
        {
            "name": "db-01",
            "vsphere_networks": ["VM Network", "DB Backend"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "db-backend-nad",
        },
        {
            "name": "edge-01",
            "vsphere_networks": ["DMZ"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "dmz-nad",
        },
    ]
    netmap = _docs(generate_wave_yaml(ctx, vms))[0]
    sources = [e["source"]["name"] for e in netmap["spec"]["map"]]
    assert sorted(sources) == ["DB Backend", "DMZ", "VM Network"]


def test_plan_type_defaults_to_cold_and_provider_pair(ctx, two_vms_one_network_one_datastore):
    """``warm`` is deprecated in favour of ``type`` (MTV-GROUNDING §4), and cold
    is the default because warm needs CBT + VMware Tools on every VM."""
    plan = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))[2]
    assert plan["spec"]["type"] == "cold"
    assert "warm" not in plan["spec"]
    assert plan["spec"]["provider"]["source"]["name"] == "vmware-prod"
    assert plan["spec"]["provider"]["destination"]["name"] == "ocpv-host"


def test_plan_description_is_wave_rationale(ctx, two_vms_one_network_one_datastore):
    plan = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))[2]
    assert "DB Backend portgroup" in plan["spec"]["description"]


def test_plan_lists_wave_vms_in_input_order(ctx, two_vms_one_network_one_datastore):
    plan = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))[2]
    assert [v["name"] for v in plan["spec"]["vms"]] == ["db-prod-01", "db-prod-02"]
    # Both VMs share the plan-level target namespace, so neither needs a per-VM
    # override in the YAML.
    assert all("namespace" not in v for v in plan["spec"]["vms"])


def test_wave_spanning_two_target_namespaces_is_refused(ctx):
    """``vms[].namespace`` is the SOURCE namespace for OpenShift providers, not a
    per-VM target override (MTV-GROUNDING §4). One Plan = one targetNamespace."""
    vms = [
        {
            "name": "primary",
            "vsphere_networks": ["VM Network"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "nad-a",
        },
        {
            "name": "side-tenant",
            "vsphere_networks": ["VM Network"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "treasury-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "nad-a",
        },
    ]
    with pytest.raises(MTVGenerationError, match="2 target namespaces"):
        generate_wave_yaml(ctx, vms)


def test_missing_target_namespace_falls_back_to_default(ctx):
    vms = [
        {
            "name": "stray",
            "vsphere_networks": ["VM Network"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "nad-a",
        },
    ]
    plan = _docs(generate_wave_yaml(ctx, vms))[2]
    assert plan["spec"]["targetNamespace"] == "finance-prod"


def test_unmapped_network_is_refused_not_sent_to_the_pod_network(ctx):
    vms = [
        {
            "name": "stray",
            "vsphere_networks": ["VM Network"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "",
        },
    ]
    with pytest.raises(MTVGenerationError, match="'VM Network'.*has no target"):
        generate_wave_yaml(ctx, vms)


def test_empty_wave_raises():
    ctx = WaveContext.from_settings(plan_id=1, wave_number=1, rationale="")
    with pytest.raises(MTVGenerationError, match="empty wave"):
        generate_wave_yaml(ctx, [])


def test_wave_with_no_networks_raises(ctx):
    vms = [
        {
            "name": "stray",
            "vsphere_networks": [],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "nad-a",
        },
    ]
    with pytest.raises(MTVGenerationError, match="no source networks"):
        generate_wave_yaml(ctx, vms)


def test_wave_without_storage_class_raises(ctx):
    vms = [
        {
            "name": "stray",
            "vsphere_networks": ["VM Network"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "",
            "target_network_attachment": "nad-a",
        },
    ]
    # The pre-check catches this with a VM-named message before the
    # downstream empty-map error fires; both are valid signals but the
    # new shape is what the operator sees.
    with pytest.raises(MTVGenerationError) as excinfo:
        generate_wave_yaml(ctx, vms)
    assert "stray" in str(excinfo.value)
    assert "target storage class" in str(excinfo.value)


# --------------------------------------------------------------------------
# Identity, access mode, migration type — grounded in docs/MTV-GROUNDING.md
# --------------------------------------------------------------------------
def _one_vm(**over):
    return {
        "name": "APP-DB-01.corp.local",
        "vsphere_networks": ["VM Network"],
        "vsphere_datastores": ["ds1"],
        "target_namespace": "prod",
        "target_storage_class": "ocs",
        "target_network_attachment": "nad-a",
        **over,
    }


def test_vm_id_is_the_moref_and_name_is_the_source_name_verbatim(ctx):
    plan = _docs(generate_wave_yaml(ctx, [_one_vm(moref="vm-4711")]))[2]
    assert plan["spec"]["vms"] == [{"id": "vm-4711", "name": "APP-DB-01.corp.local"}]


def test_vm_without_moref_emits_name_only(ctx):
    """A display name in ``id`` makes MTV fail the lookup instead of falling
    back to ``name`` — so no id at all when we don't have the real one."""
    plan = _docs(generate_wave_yaml(ctx, [_one_vm()]))[2]
    assert plan["spec"]["vms"] == [{"name": "APP-DB-01.corp.local"}]


def test_warm_is_opt_in(two_vms_one_network_one_datastore):
    warm = WaveContext.from_settings(plan_id=1, wave_number=1, rationale="", migration_type="warm")
    plan = _docs(generate_wave_yaml(warm, two_vms_one_network_one_datastore))[2]
    assert plan["spec"]["type"] == "warm"


def test_storage_access_mode_reaches_the_storagemap(ctx):
    resolver = MappingResolver(
        network_mappings=[
            {
                "source_network": "VM Network",
                "target_network_name": "nad-a",
                "target_network_type": "nad",
            }
        ],
        storage_mappings=[
            {
                "source_datastore": "ds1",
                "target_storage_class": "ocs",
                "access_mode": "ReadWriteMany",
            },
            {"source_datastore": "ds2", "target_storage_class": "ocs", "access_mode": "bogus"},
        ],
        namespace_mappings=[{"criteria": "default", "target_namespace": "prod"}],
    )
    vm = _one_vm(vsphere_datastores=["ds1", "ds2"])
    netmap, storagemap, _plan = _docs(generate_wave_yaml(ctx, [vm], resolver=resolver))
    by_src = {e["source"]["name"]: e["destination"] for e in storagemap["spec"]["map"]}
    assert by_src["ds1"] == {"storageClass": "ocs", "accessMode": "ReadWriteMany"}
    assert by_src["ds2"] == {"storageClass": "ocs"}  # not in the CRD enum → not emitted
    # NAD without its own namespace lands in the VM's target namespace
    assert netmap["spec"]["map"][0]["destination"]["namespace"] == "prod"
