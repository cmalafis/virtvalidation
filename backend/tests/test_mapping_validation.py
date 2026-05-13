"""Stage 0 mapping coverage tests."""

from __future__ import annotations

from app.core.mapping_validation import validate_plan_inputs
from app.models.target import ResourceMapping
from app.models.vm import VM


def _vm(
    name: str,
    *,
    networks=(),
    datastores=(),
    target_namespace: str | None = None,
    target_storage_class: str | None = None,
    target_network_attachment: str | None = None,
    vid: int = 1,
) -> VM:
    return VM(
        id=vid,
        name=name,
        source_hostname=name,
        vsphere_networks=list(networks),
        vsphere_datastores=list(datastores),
        target_namespace=target_namespace,
        target_storage_class=target_storage_class,
        target_network_attachment=target_network_attachment,
    )


def _mapping(
    *,
    networks=(),
    datastores=(),
    namespaces=(),
) -> ResourceMapping:
    return ResourceMapping(
        name="m",
        vcenter_source_id=1,
        ocp_target_id=1,
        network_mappings=[{"source_network": s, "target_network_name": t} for s, t in networks],
        storage_mappings=[
            {"source_datastore": s, "target_storage_class": t} for s, t in datastores
        ],
        namespace_mappings=list(namespaces) if namespaces else [],
    )


class TestStage0Validation:
    def test_complete_mapping_ok(self):
        vm = _vm(
            "app-01",
            networks=("vlan-100",),
            datastores=("tier1",),
            target_namespace="prod",
        )
        m = _mapping(
            networks=[("vlan-100", "vlan-100-nad")],
            datastores=[("tier1", "ocs-rbd")],
        )
        result = validate_plan_inputs([vm], [m])
        assert result.ok
        assert result.gaps == []

    def test_missing_network_reported(self):
        vm = _vm(
            "app-01",
            networks=("vlan-999",),
            datastores=(),
            target_namespace="prod",
        )
        m = _mapping(networks=[])
        result = validate_plan_inputs([vm], [m])
        assert not result.ok
        assert any(g.kind == "network" and g.source_value == "vlan-999" for g in result.gaps)

    def test_missing_datastore_reported(self):
        vm = _vm("app-01", datastores=("tierX",), target_namespace="ns")
        m = _mapping()
        result = validate_plan_inputs([vm], [m])
        assert any(g.kind == "datastore" and g.source_value == "tierX" for g in result.gaps)

    def test_missing_namespace_reported(self):
        vm = _vm("app-01")
        m = _mapping()
        result = validate_plan_inputs([vm], [m])
        # No mapping namespaces + no vm.target_namespace → namespace gap.
        assert any(g.kind == "namespace" for g in result.gaps)

    def test_vm_level_fallback_satisfies_network(self):
        # The mapping doesn't carry vlan-X but the VM has a per-row NAD;
        # the MTV emitter accepts this fallback.
        vm = _vm(
            "app-01",
            networks=("vlan-X",),
            target_namespace="ns",
            target_network_attachment="vlan-X-nad",
        )
        m = _mapping()
        result = validate_plan_inputs([vm], [m])
        gaps = [g for g in result.gaps if g.kind == "network"]
        assert gaps == []

    def test_empty_mappings_makes_everything_a_gap(self):
        # Operator opted out of mapping-driven coverage entirely
        # (``mapping_ids=[]``). Without per-VM target fields, every
        # source resource becomes a gap — same as the old None case.
        vm = _vm(
            "app-01",
            networks=("vlan-100",),
            datastores=("tier1",),
        )
        result = validate_plan_inputs([vm], [])
        kinds = {g.kind for g in result.gaps}
        assert "network" in kinds
        assert "datastore" in kinds
        assert "namespace" in kinds

    def test_render_truncates_long_lists(self):
        vms = [
            _vm(f"app-{i}", datastores=("tier-x",), vid=i, target_namespace="ns")
            for i in range(1, 12)
        ]
        result = validate_plan_inputs(vms, [_mapping()])
        rendered = result.render(max_items=3)
        assert "and 8 more" in rendered  # 11 gaps total, 3 shown

    def test_per_vcenter_routing_picks_matching_mapping(self):
        # Two mappings, one per vCenter. Each VM should validate against
        # the mapping covering its own vCenter — the wrong-vcenter
        # mapping's nets/datastores must NOT count for the other VM.
        vm_a = _vm(
            "app-a",
            networks=("vlan-a",),
            datastores=("tier-a",),
            target_namespace="ns-a",
            vid=1,
        )
        vm_a.source_vcenter_id = 1
        vm_b = _vm(
            "app-b",
            networks=("vlan-b",),
            datastores=("tier-b",),
            target_namespace="ns-b",
            vid=2,
        )
        vm_b.source_vcenter_id = 2
        m_a = _mapping(
            networks=[("vlan-a", "nad-a")],
            datastores=[("tier-a", "sc-a")],
        )
        m_a.vcenter_source_id = 1
        m_b = ResourceMapping(
            name="m-b",
            vcenter_source_id=2,
            ocp_target_id=1,
            network_mappings=[{"source_network": "vlan-b", "target_network_name": "nad-b"}],
            storage_mappings=[{"source_datastore": "tier-b", "target_storage_class": "sc-b"}],
            namespace_mappings=[],
        )
        result = validate_plan_inputs([vm_a, vm_b], [m_a, m_b])
        assert result.ok, result.render()

    def test_missing_mapping_for_vcenter_emits_no_mapping_gap(self):
        # VM's vCenter isn't covered by any selected mapping → single
        # ``no_mapping`` gap, not per-resource gaps.
        vm = _vm(
            "app-a",
            networks=("vlan-a",),
            datastores=("tier-a",),
            target_namespace="ns",
            vid=1,
        )
        vm.source_vcenter_id = 99
        m_other = _mapping()  # vcenter_source_id=1, doesn't cover vm
        result = validate_plan_inputs([vm], [m_other])
        assert not result.ok
        kinds = {g.kind for g in result.gaps}
        assert kinds == {"no_mapping"}
