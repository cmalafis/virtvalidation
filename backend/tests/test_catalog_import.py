"""Catalog import from pasted ``oc get … -o yaml`` (app.core.catalog_import)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.catalog_import import CatalogImportError, parse_cluster_yaml, plan_import
from app.models.target import OCPTarget

SAMPLE = (Path(__file__).parent / "fixtures" / "cluster_export" / "ocp422_sample.yaml").read_text()


def _by_name(items):
    return {i.name: i for i in items}


def test_parses_udn_cudn_and_nads():
    parsed = parse_cluster_yaml(SAMPLE)
    nets = _by_name(parsed.networks)
    # The CUDN-owned NAD is described by its CUDN, not imported twice.
    assert set(nets) == {
        "tenant-blue",
        "vlan-200",
        "finance-primary",
        "db-vlan-100",
        "storage-sriov",
    }

    blue = nets["tenant-blue"]
    assert (blue.network_type, blue.role, blue.topology, blue.ipam_mode) == (
        "cudn",
        "primary",
        "layer2",
        "persistent",
    )
    assert blue.subnets == ["10.20.0.0/16"]
    assert blue.binding == "l2bridge"

    loc = nets["vlan-200"]
    assert (loc.network_type, loc.role, loc.topology, loc.ipam_mode) == (
        "cudn",
        "secondary",
        "localnet",
        "disabled",
    )
    assert loc.physical_network_name == "physnet-vms"
    assert loc.vlan_id == 200

    udn = nets["finance-primary"]
    assert (udn.network_type, udn.namespace, udn.role) == ("udn", "finance-prod", "primary")

    br = nets["db-vlan-100"]
    assert (br.network_type, br.topology, br.vlan_id, br.binding) == (
        "nad",
        "bridge",
        100,
        "bridge",
    )
    assert nets["storage-sriov"].topology == "sriov"


def test_primary_udn_namespaces_from_udn_and_cudn_status():
    parsed = parse_cluster_yaml(SAMPLE)
    assert parsed.primary_udn_namespaces == {
        "finance-prod": "finance-primary",
        "blue-prod": "tenant-blue",
        "blue-dev": "tenant-blue",
    }


def test_storage_classes_profile_wins_over_heuristic():
    parsed = parse_cluster_yaml(SAMPLE)
    scs = _by_name(parsed.storage_classes)
    rbd = scs["ocs-storagecluster-ceph-rbd-virtualization"]
    assert (rbd.volume_mode, rbd.access_mode, rbd.is_default) == ("Block", "ReadWriteMany", True)
    assert scs["nfs-csi"].access_mode == "ReadWriteMany"
    # gp3's StorageProfile says Filesystem, overriding the EBS heuristic.
    assert (scs["gp3-csi"].volume_mode, scs["gp3-csi"].access_mode) == (
        "Filesystem",
        "ReadWriteOnce",
    )


def test_bridge_mappings_and_localnet_warning():
    parsed = parse_cluster_yaml(SAMPLE)
    assert parsed.localnet_bridge_mappings == ["physnet-vms"]
    assert parsed.warnings == []
    broken = SAMPLE.replace("physicalNetworkName: physnet-vms", "physicalNetworkName: physnet-typo")
    assert any("physnet-typo" in w for w in parse_cluster_yaml(broken).warnings)


def test_cudn_selector_matches_pasted_namespaces():
    text = """
apiVersion: k8s.ovn.org/v1
kind: ClusterUserDefinedNetwork
metadata: {name: green}
spec:
  namespaceSelector: {matchLabels: {tenant: green}}
  network:
    topology: Layer2
    layer2: {role: Primary, subnets: [10.9.0.0/16], ipam: {lifecycle: Persistent}}
---
apiVersion: v1
kind: Namespace
metadata:
  name: green-a
  labels: {tenant: green, k8s.ovn.org/primary-user-defined-network: ""}
---
apiVersion: v1
kind: Namespace
metadata: {name: other, labels: {tenant: red}}
"""
    assert parse_cluster_yaml(text).primary_udn_namespaces == {"green-a": "green"}


def test_duplicate_names_keep_first_and_warn():
    text = """
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata: {name: dup, namespace: a}
spec: {config: '{"type":"bridge","vlan":5}'}
---
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata: {name: dup, namespace: b}
spec: {config: '{"type":"bridge","vlan":6}'}
"""
    parsed = parse_cluster_yaml(text)
    assert [n.namespace for n in parsed.networks] == ["a"]
    assert any("more than once" in w for w in parsed.warnings)


def test_invalid_yaml_raises():
    with pytest.raises(CatalogImportError):
        parse_cluster_yaml("kind: [unterminated")


def test_plan_import_diff_never_clears_fields():
    parsed = parse_cluster_yaml(SAMPLE)
    preview = plan_import(
        parsed,
        current_networks={
            "db-vlan-100": {
                "network_type": "nad",
                "namespace": "finance-prod",
                "topology": "bridge",
                "vlan_id": 100,
                "binding": "bridge",
                "role": "secondary",
                "mtu": 9000,  # set by the operator, absent in the paste
            }
        },
        current_storage={},
        current_namespaces={"blue-prod": None},
    )
    actions = {r["name"]: r["action"] for r in preview["networks"]}
    assert actions["db-vlan-100"] == "unchanged"
    assert actions["tenant-blue"] == "create"
    ns = {r["name"]: r["action"] for r in preview["namespaces"]}
    assert ns["blue-prod"] == "update" and ns["blue-dev"] == "create"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture
def target(db_session):
    t = OCPTarget(name="ocp422", api_endpoint="https://api.example:6443", ocp_version="4.22")
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def test_import_api_dry_run_then_apply(client, target):
    url = f"/api/ocp-targets/{target.id}/catalog/import"
    r = client.post(url, json={"yaml": SAMPLE})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["applied"] is False
    assert client.get(f"/api/ocp-targets/{target.id}/networks").json() == []

    r = client.post(url, json={"yaml": SAMPLE, "dry_run": False})
    assert r.status_code == 200, r.text
    nets = {n["name"]: n for n in client.get(f"/api/ocp-targets/{target.id}/networks").json()}
    assert nets["tenant-blue"]["role"] == "primary"
    assert nets["vlan-200"]["physical_network_name"] == "physnet-vms"
    scs = {s["name"]: s for s in client.get(f"/api/ocp-targets/{target.id}/storage-classes").json()}
    assert scs["ocs-storagecluster-ceph-rbd-virtualization"]["is_default"] is True
    assert scs["ocs-storagecluster-ceph-rbd-virtualization"]["volume_mode"] == "Block"
    ns = client.get(f"/api/ocp-targets/{target.id}/namespaces").json()
    ns_items = ns["items"] if isinstance(ns, dict) else ns
    assert {n["name"]: n["primary_udn_network"] for n in ns_items}["blue-dev"] == "tenant-blue"
    tgt = client.get(f"/api/sources/targets/{target.id}").json()
    assert tgt["localnet_bridge_mappings"] == ["physnet-vms"]

    # Re-applying the same paste is a no-op.
    again = client.post(url, json={"yaml": SAMPLE}).json()
    assert {r["action"] for r in again["networks"]} == {"unchanged"}


def test_import_api_rejects_garbage(client, target):
    r = client.post(f"/api/ocp-targets/{target.id}/catalog/import", json={"yaml": "a: [b"})
    assert r.status_code == 422
