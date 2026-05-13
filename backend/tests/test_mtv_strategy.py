"""Tests for the namespace-strategy and unmapped-resource pre-check
added in the mapping-editor overhaul.

The legacy criteria-row schema and the new NamespaceStrategy dict
must both work — the resolver dispatches on shape so old plans
keep emitting valid YAML."""

from __future__ import annotations

import pytest

from app.core.mtv import (
    MappingResolver,
    MTVGenerationError,
    WaveContext,
    generate_wave_yaml,
)


@pytest.fixture
def ctx() -> WaveContext:
    return WaveContext(
        plan_id=1,
        wave_number=1,
        rationale="wave one",
        namespace="openshift-mtv",
        source_provider="vmware",
        destination_provider="ocpv",
        default_target_namespace="default-ns",
    )


def _vm(name: str, env: str = "", app: str = "", folder: str = "") -> dict:
    return {
        "name": name,
        "vsphere_networks": ["prod-vlan-100"],
        "vsphere_datastores": ["ds-prod-01"],
        "environment": env,
        "application_hint": app,
        "vcenter_folder": folder,
    }


def _basic_resolver(strategy_dict_or_list) -> MappingResolver:
    return MappingResolver(
        network_mappings=[
            {
                "source_network": "prod-vlan-100",
                "target_network_name": "tenant-prod-net",
                "target_network_type": "nad",
                "target_namespace": "openshift-multus",
            }
        ],
        storage_mappings=[
            {
                "source_datastore": "ds-prod-01",
                "target_storage_class": "ocs-rbd",
            }
        ],
        namespace_mappings=strategy_dict_or_list,
    )


# ---------------------------------------------------------------------------
# Strategy resolution
# ---------------------------------------------------------------------------
def test_single_strategy_resolves_every_vm_to_one_namespace():
    r = _basic_resolver({"strategy": "single", "single_namespace": "all-migrated"})
    assert r.resolve_namespace(_vm("a")) == "all-migrated"
    assert r.resolve_namespace(_vm("b", env="prod")) == "all-migrated"


def test_per_environment_strategy_routes_by_env():
    r = _basic_resolver(
        {
            "strategy": "per_environment",
            "per_env_namespaces": {
                "production": "prod-vms",
                "staging": "stg-vms",
                "development": "dev-vms",
            },
        }
    )
    assert r.resolve_namespace(_vm("a", env="production")) == "prod-vms"
    assert r.resolve_namespace(_vm("b", env="STAGING")) == "stg-vms"
    # Unknown environment falls through to None — caller fills in
    # default_target_namespace.
    assert r.resolve_namespace(_vm("c", env="qa")) is None


def test_per_application_strategy_derives_dns_safe_namespace():
    r = _basic_resolver({"strategy": "per_application", "per_app_prefix": "app"})
    assert r.resolve_namespace(_vm("a", app="Epic EMR")) == "app-epic-emr-vms"
    assert r.resolve_namespace(_vm("b", app="athena-billing!")) == "app-athena-billing-vms"
    # Missing application tag — operators get a catch-all bucket.
    assert r.resolve_namespace(_vm("c")) == "unassigned-vms"


def test_legacy_list_shape_still_works():
    """Plans created before the strategy refactor stored
    namespace_mappings as a list of criteria rows. Reading them must
    keep working so existing MTV YAML exports don't break."""
    r = _basic_resolver(
        [
            {
                "criteria": "environment",
                "criteria_value": "production",
                "target_namespace": "prod-vms",
            },
            {"criteria": "default", "target_namespace": "default-vms"},
        ]
    )
    assert r.resolve_namespace(_vm("a", env="production")) == "prod-vms"
    assert r.resolve_namespace(_vm("b", env="dev")) == "default-vms"


# ---------------------------------------------------------------------------
# generate_wave_yaml unmapped-resource pre-check
# ---------------------------------------------------------------------------
def test_unmapped_source_network_raises_clear_error(ctx):
    """If a VM references a source network the mapping doesn't
    resolve, generation must fail with the exact unmapped names so
    the operator can fix the mapping editor."""
    resolver = MappingResolver(
        network_mappings=[],
        storage_mappings=[{"source_datastore": "ds-1", "target_storage_class": "ocs-rbd"}],
        namespace_mappings={"strategy": "single", "single_namespace": "ns"},
    )
    vms = [
        {
            "name": "vm-1",
            "vsphere_networks": ["prod-vlan-100"],
            "vsphere_datastores": ["ds-1"],
        }
    ]
    with pytest.raises(MTVGenerationError, match="prod-vlan-100"):
        generate_wave_yaml(ctx, vms, resolver=resolver)


def test_unmapped_source_datastore_raises_clear_error(ctx):
    resolver = MappingResolver(
        network_mappings=[
            {
                "source_network": "n1",
                "target_network_name": "tenant-net",
                "target_network_type": "nad",
                "target_namespace": "ns",
            }
        ],
        storage_mappings=[],
        namespace_mappings={"strategy": "single", "single_namespace": "ns"},
    )
    vms = [{"name": "vm-1", "vsphere_networks": ["n1"], "vsphere_datastores": ["ds-x"]}]
    with pytest.raises(MTVGenerationError, match="ds-x"):
        generate_wave_yaml(ctx, vms, resolver=resolver)


def test_fully_mapped_wave_emits_real_target_names(ctx):
    """The happy path: every source resource is mapped, and the
    emitted YAML references the actual target_network_name and
    target_storage_class — no placeholders, no fallbacks."""
    resolver = _basic_resolver({"strategy": "single", "single_namespace": "tenant-prod"})
    vms = [
        {
            "name": "vm-1",
            "vsphere_networks": ["prod-vlan-100"],
            "vsphere_datastores": ["ds-prod-01"],
        }
    ]
    yaml_text = generate_wave_yaml(ctx, vms, resolver=resolver)
    assert "tenant-prod-net" in yaml_text
    assert "ocs-rbd" in yaml_text
    assert "tenant-prod" in yaml_text
    # No placeholder text leaks through.
    assert "<TARGET_NETWORK>" not in yaml_text


def test_no_resolver_still_falls_back_to_per_vm_fields(ctx):
    """Plans predating target-cluster registration pass resolver=None
    and rely on per-VM target_* fields. That path must keep working."""
    vms = [
        {
            "name": "vm-1",
            "vsphere_networks": ["prod-vlan-100"],
            "vsphere_datastores": ["ds-prod-01"],
            "target_namespace": "legacy-ns",
            "target_network_attachment": "legacy-nad",
            "target_storage_class": "legacy-sc",
        }
    ]
    yaml_text = generate_wave_yaml(ctx, vms, resolver=None)
    assert "legacy-nad" in yaml_text
    assert "legacy-sc" in yaml_text
