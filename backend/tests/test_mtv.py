"""Tests for app.core.mtv — wave-to-MTV-YAML conversion."""

from __future__ import annotations

import pytest
import yaml

from app.core.mtv import MTVGenerationError, WaveContext, generate_wave_yaml


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


def test_plan_includes_warm_true_and_provider_pair(ctx, two_vms_one_network_one_datastore):
    plan = _docs(generate_wave_yaml(ctx, two_vms_one_network_one_datastore))[2]
    assert plan["spec"]["warm"] is True
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


def test_plan_emits_per_vm_namespace_override_when_diverging(ctx):
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
    plan = _docs(generate_wave_yaml(ctx, vms))[2]
    plan_target = plan["spec"]["targetNamespace"]
    assert plan_target == "finance-prod"
    by_name = {v["name"]: v for v in plan["spec"]["vms"]}
    assert "namespace" not in by_name["primary"]
    assert by_name["side-tenant"]["namespace"] == "treasury-prod"


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


def test_missing_nad_falls_back_to_pod_network(ctx):
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
    netmap = _docs(generate_wave_yaml(ctx, vms))[0]
    assert netmap["spec"]["map"][0]["destination"] == {"type": "pod"}


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
    with pytest.raises(MTVGenerationError, match="storageclass mappings"):
        generate_wave_yaml(ctx, vms)
