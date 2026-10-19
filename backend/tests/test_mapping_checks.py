"""Deterministic, version-aware mapping checks (app.core.mapping_checks)."""

from __future__ import annotations

from types import SimpleNamespace

from app.core.mapping_catalog import CatalogSnapshot
from app.core.mapping_checks import VMView, check_mapping, vm_view, wave_mapping_view
from app.core.mtv import MappingResolver


def _catalog(**kw) -> CatalogSnapshot:
    base = dict(
        ocp_target_id=1,
        ocp_version="4.22",
        localnet_bridge_mappings=["physnet-vms"],
        networks={
            "tenant": {
                "network_type": "cudn",
                "role": "primary",
                "topology": "layer2",
                "ipam_mode": "persistent",
                "subnets": ["10.20.0.0/16"],
            },
            "vlan-200": {
                "network_type": "cudn",
                "role": "secondary",
                "topology": "localnet",
                "physical_network_name": "physnet-vms",
                "ipam_mode": "disabled",
            },
        },
        storage_classes={
            "rbd": {"access_mode": "ReadWriteMany", "volume_mode": "Block"},
            "gp3": {"access_mode": "ReadWriteOnce"},
        },
        namespaces={"blue-prod": "tenant", "plain-ns": None},
    )
    base.update(kw)
    return CatalogSnapshot(**base)


NETS = [
    {"source_network": "App", "target_network_name": "tenant"},
    {"source_network": "VLAN200", "target_network_name": "vlan-200"},
]
STORE = [{"source_datastore": "ds1", "target_storage_class": "rbd"}]


def _codes(findings):
    return {f.code: f for f in findings}


def test_clean_mapping_only_carries_the_udn_support_note():
    vm = VMView("app-01", ("App",), ("ds1",), (("App", "10.20.4.5"),), "blue-prod")
    findings = check_mapping(
        network_mappings=NETS, storage_mappings=STORE, catalog=_catalog(), vms=[vm]
    )
    # Primary-UDN destinations are still Tech Preview for MTV.
    assert [(f.code, f.severity) for f in findings] == [("udn.mtv_destination_support", "warning")]


def test_static_ip_outside_udn_subnet_is_blocking():
    vm = VMView("app-02", ("App",), ("ds1",), (("App", "172.16.0.9"),), "blue-prod")
    f = _codes(
        check_mapping(network_mappings=NETS, storage_mappings=STORE, catalog=_catalog(), vms=[vm])
    )
    hit = f["udn.static_ip_outside_subnet"]
    assert hit.severity == "blocking" and hit.vm_names == ["app-02"]
    # Not a problem when the plan doesn't preserve static IPs.
    f2 = check_mapping(
        network_mappings=NETS,
        storage_mappings=STORE,
        catalog=_catalog(),
        vms=[vm],
        preserve_static_ips=False,
    )
    assert "udn.static_ip_outside_subnet" not in _codes(f2)


def test_vm_landing_in_non_udn_namespace_is_blocking():
    vm = VMView("app-03", ("App",), ("ds1",), (), "plain-ns")
    f = _codes(
        check_mapping(network_mappings=NETS, storage_mappings=STORE, catalog=_catalog(), vms=[vm])
    )
    assert f["udn.namespace_not_primary"].severity == "blocking"


def test_primary_udn_shape_rules():
    cat = _catalog()
    cat.networks["tenant"].update(topology="layer3", ipam_mode="none", binding="passt")
    f = _codes(check_mapping(network_mappings=NETS, storage_mappings=STORE, catalog=cat))
    assert f["udn.primary_not_layer2"].severity == "blocking"
    assert f["udn.ipam_not_persistent"].severity == "blocking"
    assert f["udn.passt_tech_preview"].severity == "warning"


def test_localnet_bridge_mapping_and_ipam():
    cat = _catalog(localnet_bridge_mappings=["other"])
    cat.networks["vlan-200"]["ipam_mode"] = "persistent"
    f = _codes(check_mapping(network_mappings=NETS, storage_mappings=STORE, catalog=cat))
    assert f["localnet.bridge_mapping_missing"].severity == "blocking"
    assert f["localnet.ipam_enabled"].severity == "blocking"
    # Never imported → no bridge-mapping verdict.
    f2 = _codes(
        check_mapping(
            network_mappings=NETS,
            storage_mappings=STORE,
            catalog=_catalog(localnet_bridge_mappings=None),
        )
    )
    assert "localnet.bridge_mapping_missing" not in f2


def test_evpn_needs_422():
    cat = _catalog(ocp_version="4.21")
    cat.networks["tenant"]["transport"] = "evpn"
    f = _codes(check_mapping(network_mappings=NETS, storage_mappings=STORE, catalog=cat))
    assert f["udn.evpn_unavailable"].severity == "blocking"
    cat422 = _catalog()
    cat422.networks["tenant"]["transport"] = "evpn"
    f = _codes(check_mapping(network_mappings=NETS, storage_mappings=STORE, catalog=cat422))
    assert f["udn.evpn_support_level"].severity == "warning"


def test_rwo_storage_warns_about_live_migration():
    f = _codes(
        check_mapping(
            network_mappings=[],
            storage_mappings=[{"source_datastore": "d", "target_storage_class": "gp3"}],
            catalog=_catalog(),
        )
    )
    assert f["storage.rwo_no_live_migration"].severity == "warning"


def test_copy_offload_support_by_mtv_version():
    cat = _catalog(ocp_version="4.21")  # pairs with MTV 2.11: cold GA, warm TP
    cat.storage_classes["rbd"].update(copy_offload_vendor="infinibox", copy_offload_secret="s")
    cold = _codes(check_mapping(network_mappings=[], storage_mappings=STORE, catalog=cat))
    assert "offload.support_level" not in cold
    assert cold["offload.vendor_developer_preview"].severity == "warning"
    warm = _codes(
        check_mapping(
            network_mappings=[], storage_mappings=STORE, catalog=cat, migration_type="warm"
        )
    )
    assert "TP" in warm["offload.support_level"].message
    nfs = _codes(
        check_mapping(
            network_mappings=[], storage_mappings=STORE, catalog=cat, datastore_types={"ds1": "NFS"}
        )
    )
    assert nfs["offload.nfs_datastore"].severity == "warning"


def test_unknown_version_is_info():
    vm = VMView("x" * 48)
    f = _codes(
        check_mapping(
            network_mappings=[], storage_mappings=[], catalog=_catalog(ocp_version=None), vms=[vm]
        )
    )
    assert f["target.version_unknown"].severity == "info"


def test_vm_view_reads_nic_ips():
    vm = SimpleNamespace(
        name="a",
        vsphere_networks=["App"],
        vsphere_datastores=["ds1"],
        ip_address=None,
        hardware_facts={"nics": [{"network": "App", "ipv4": "10.20.0.4, 10.20.0.5"}]},
    )
    assert vm_view(vm).nic_ips == (("App", "10.20.0.4"), ("App", "10.20.0.5"))


def test_wave_mapping_view_summary():
    resolver = MappingResolver(
        network_mappings=NETS + [{"source_network": "Backup", "ignored": True}],
        storage_mappings=STORE,
        namespace_mappings={"strategy": "single", "single_namespace": "blue-prod"},
        catalog=_catalog(),
    )
    vms = [
        SimpleNamespace(
            name="a",
            vsphere_networks=["App", "Backup"],
            vsphere_datastores=["ds1"],
            hardware_facts={},
            ip_address=None,
            target_namespace_override=None,
            environment="",
            application_hint="",
            vsphere_folder="",
        ),
        SimpleNamespace(
            name="b",
            vsphere_networks=["VLAN200"],
            vsphere_datastores=["ds1"],
            hardware_facts={},
            ip_address=None,
            target_namespace_override=None,
            environment="",
            application_hint="",
            vsphere_folder="",
        ),
    ]
    summary, findings = wave_mapping_view(vms, resolver)
    kinds = {r["source"]: r["kind"] for r in summary["networks"]}
    assert kinds == {"App": "primary_udn", "Backup": "ignored", "VLAN200": "localnet"}
    assert summary["storage"] == [
        {
            "source": "ds1",
            "storage_class": "rbd",
            "access_mode": "ReadWriteMany",
            "volume_mode": "Block",
            "offload": None,
            "vm_count": 2,
        }
    ]
    assert [f["code"] for f in findings] == ["udn.mtv_destination_support"]
    assert wave_mapping_view(vms, None) == ({"networks": [], "storage": []}, [])


def test_provider_ip_inside_primary_udn_subnet_is_blocking():
    f = _codes(
        check_mapping(
            network_mappings=NETS,
            storage_mappings=STORE,
            catalog=_catalog(),
            provider_address="10.20.0.10",
        )
    )
    assert f["udn.provider_ip_inside_subnet"].severity == "blocking"
    # A hostname can't be checked and is not reported.
    f2 = _codes(
        check_mapping(
            network_mappings=NETS,
            storage_mappings=STORE,
            catalog=_catalog(),
            provider_address="vc01.example.com",
        )
    )
    assert "udn.provider_ip_inside_subnet" not in f2
