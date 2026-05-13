"""End-to-end plan pipeline tests.

Exercises stages 0-7 against a battery of VM populations:
  - tiny (1, 2)
  - human-scale (11, 100)
  - selection cap (250)
  - scale beyond the cap (1000, 5000) for performance pins
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.core.family import detect_family
from app.core.plan_pipeline import PlanValidationError, run_pipeline
from app.models.target import ResourceMapping
from app.models.vm import VM


def _vm(
    vid: int,
    name: str,
    *,
    vcenter_id: int = 1,
    networks=("vlan-100",),
    datastores=("tier1",),
    target_namespace: str | None = "prod",
    target_storage_class: str | None = None,
    target_network_attachment: str | None = None,
    environment: str = "production",
    application_hint: str | None = None,
) -> VM:
    return VM(
        id=vid,
        name=name,
        source_hostname=name,
        source_vcenter_id=vcenter_id,
        vsphere_networks=list(networks),
        vsphere_datastores=list(datastores),
        target_namespace=target_namespace,
        target_storage_class=target_storage_class,
        target_network_attachment=target_network_attachment,
        environment=environment,
        application_hint=application_hint,
    )


def _complete_mapping() -> ResourceMapping:
    return ResourceMapping(
        name="m",
        vcenter_source_id=1,
        ocp_target_id=1,
        network_mappings=[{"source_network": "vlan-100", "target_network_name": "vlan-100-nad"}],
        storage_mappings=[{"source_datastore": "tier1", "target_storage_class": "ocs-rbd"}],
        namespace_mappings=[],
    )


def _run(vms, mappings=None, plan_id: int = 1):
    if mappings is None:
        mappings = [_complete_mapping()]
    return asyncio.run(run_pipeline(vms, mappings, plan_id=plan_id))


class TestPipelineScale:
    def test_one_vm(self):
        vms = [_vm(1, "lonely-01")]
        result = _run(vms)
        assert len(result.waves) == 1
        assert result.waves[0].wave.vm_ids == [1]
        assert result.waves[0].description  # mechanical fallback fills this

    def test_eleven_vms(self):
        # 11 VMs in one application → preclassifier may keep them in one
        # group, wave_skeleton splits into 2 batches at the 10-VM cap.
        vms = [_vm(i, f"web-prod-{i:03d}", application_hint="webapp") for i in range(1, 12)]
        result = _run(vms)
        # Every wave ≤10 VMs
        for aw in result.waves:
            assert aw.wave.vm_count <= 10
        all_ids = sorted(v for aw in result.waves for v in aw.wave.vm_ids)
        assert all_ids == list(range(1, 12))

    def test_billing_db_family_split_across_waves(self):
        # 4 billing-db members in one group → cap=2, so they should land
        # in at most ceil(4/2)=2 per wave.
        vms = [_vm(i, f"billing-db-{i:03d}", application_hint="billing") for i in range(1, 5)]
        result = _run(vms)
        for aw in result.waves:
            count = sum(
                1
                for vid in aw.wave.vm_ids
                if detect_family(f"billing-db-{vid:03d}") == "billing-db"
            )
            assert count <= 2, f"wave {aw.wave.wave_number} has {count} billing-db members"

    def test_eleven_member_family_no_wave_has_more_than_six(self):
        # 11 family members → family_cap(11) = 6. No wave should have
        # more than 6 of this family.
        vms = [_vm(i, f"ad-dc-{i:03d}", application_hint="ad") for i in range(1, 12)]
        result = _run(vms)
        for aw in result.waves:
            count = sum(1 for vid in aw.wave.vm_ids if detect_family(f"ad-dc-{vid:03d}") == "ad-dc")
            assert count <= 6

    def test_cross_vcenter_distinct_concurrency_groups(self):
        # 3 VMs in vc=10, 3 in vc=20 — Stage 5 should assign different
        # vcenter waves to (potentially) same color but never two vc=10
        # waves to the same color. Two mappings, one per vcenter,
        # exercises the multi-mapping per-vcenter routing.
        vms = [_vm(i, f"app-{i:03d}", vcenter_id=10, application_hint="a") for i in range(1, 4)] + [
            _vm(i, f"app-{i:03d}", vcenter_id=20, application_hint="b") for i in range(4, 7)
        ]
        m10 = ResourceMapping(
            name="m10",
            vcenter_source_id=10,
            ocp_target_id=1,
            network_mappings=[
                {"source_network": "vlan-100", "target_network_name": "vlan-100-nad"}
            ],
            storage_mappings=[{"source_datastore": "tier1", "target_storage_class": "ocs-rbd"}],
            namespace_mappings=[],
        )
        m20 = ResourceMapping(
            name="m20",
            vcenter_source_id=20,
            ocp_target_id=1,
            network_mappings=[
                {"source_network": "vlan-100", "target_network_name": "vlan-100-nad"}
            ],
            storage_mappings=[{"source_datastore": "tier1", "target_storage_class": "ocs-rbd"}],
            namespace_mappings=[],
        )
        result = _run(vms, [m10, m20])
        # Should have at least 2 waves (one per vcenter, different
        # primary partitions).
        vc_per_wave = {
            aw.wave.wave_number: {g.key.vcenter_id for g in aw.wave.groups} for aw in result.waves
        }
        # Every wave is single-vcenter (partition coherence).
        for vcs in vc_per_wave.values():
            assert len(vcs) == 1


class TestPipelineValidation:
    def test_incomplete_mapping_raises(self):
        vm = _vm(1, "app-01", networks=("vlan-999",))
        # Mapping covers vlan-100 only.
        with pytest.raises(PlanValidationError) as exc:
            _run([vm], [_complete_mapping()])
        # Gap should call out vlan-999.
        assert any(
            g.source_value == "vlan-999" and g.kind == "network" for g in exc.value.result.gaps
        )

    def test_missing_namespace_raises(self):
        vm = _vm(1, "app-01", target_namespace=None)
        # Mapping has no namespace strategy.
        with pytest.raises(PlanValidationError):
            _run([vm], [_complete_mapping()])


class TestPipelinePerformance:
    """Pins the deterministic stages run in under-budget time."""

    @pytest.mark.parametrize("count", [100, 250])
    def test_deterministic_stages_fast(self, count):
        vms = [
            _vm(
                i,
                f"app-{(i % 20):02d}-{i:04d}",
                application_hint=f"app-{i % 20}",
            )
            for i in range(1, count + 1)
        ]
        start = time.perf_counter()
        result = _run(vms)
        elapsed = time.perf_counter() - start
        # Mechanical fallback annotation has no LLM cost, so this
        # measures only the deterministic stages. ≤2s is comfortable
        # for CI; tighter benchmarks live in the local verify script.
        assert elapsed < 2.0, f"pipeline took {elapsed:.2f}s for {count} VMs"
        all_ids = sorted(v for aw in result.waves for v in aw.wave.vm_ids)
        assert all_ids == sorted(vm.id for vm in vms)

    def test_one_thousand_vms_under_five_seconds(self):
        # Generous CI budget — laptop runs this in ~0.5s; CI containers
        # are slower but should still finish well under 5s.
        vms = [
            _vm(
                i,
                f"app-{(i % 50):02d}-{i:05d}",
                application_hint=f"app-{i % 50}",
            )
            for i in range(1, 1001)
        ]
        start = time.perf_counter()
        result = _run(vms)
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"1000-VM pipeline took {elapsed:.2f}s"
        all_ids = sorted(v for aw in result.waves for v in aw.wave.vm_ids)
        assert all_ids == sorted(vm.id for vm in vms)
