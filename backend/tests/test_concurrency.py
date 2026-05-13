"""Stage 5 concurrency assignment tests."""

from __future__ import annotations

from app.core.concurrency import assign_concurrency_groups
from app.core.preclassifier import GroupKey, VMGroup
from app.core.wave_skeleton import Wave


def _group(vm_ids: list[int], *, vcenter_id: int, discriminator: str = "x"):
    key = GroupKey(
        vcenter_id=vcenter_id,
        target_namespace="ns",
        role="app",
        state="stateless",
        discriminator=discriminator,
        environment="production",
    )
    return VMGroup(
        key=key,
        vm_ids=sorted(vm_ids),
        shared_attributes={
            "networks": [],
            "datastores": [],
            "application_hints": [],
            "environments": [],
            "os_families": [],
        },
        estimated_role="app",
        estimated_state="stateless",
        migration_risk="low",
        dependency_hints=[],
        notes="",
        ha_members=[],
    )


def _wave(num: int, groups):
    return Wave(wave_number=num, groups=list(groups), estimated_risk="low")


class TestConcurrencyAssignment:
    def test_different_vcenters_share_color(self):
        w1 = _wave(1, [_group([1], vcenter_id=10)])
        w2 = _wave(2, [_group([2], vcenter_id=20)])
        names = {1: "app-a-01", 2: "other-b-01"}
        assign_concurrency_groups([w1, w2], names)
        # Disjoint vCenters + disjoint families → same color.
        assert w1.concurrency_group_id == w2.concurrency_group_id

    def test_same_vcenter_distinct_colors(self):
        w1 = _wave(1, [_group([1], vcenter_id=10)])
        w2 = _wave(2, [_group([2], vcenter_id=10, discriminator="y")])
        names = {1: "app-a-01", 2: "other-b-01"}
        assign_concurrency_groups([w1, w2], names)
        assert w1.concurrency_group_id != w2.concurrency_group_id

    def test_shared_family_distinct_colors(self):
        # Different vCenters but the family key collides (both VMs
        # in family "billing-db") → can't run concurrently.
        w1 = _wave(1, [_group([1], vcenter_id=10)])
        w2 = _wave(2, [_group([2], vcenter_id=20)])
        names = {1: "billing-db-01", 2: "billing-db-02"}
        assign_concurrency_groups([w1, w2], names)
        assert w1.concurrency_group_id != w2.concurrency_group_id

    def test_three_waves_two_colors_when_pairwise_conflicts(self):
        # w1 vc=10, w2 vc=10 (conflict with w1), w3 vc=20 (compatible with w1)
        w1 = _wave(1, [_group([1], vcenter_id=10)])
        w2 = _wave(2, [_group([2], vcenter_id=10, discriminator="y")])
        w3 = _wave(3, [_group([3], vcenter_id=20)])
        names = {1: "a-1", 2: "b-1", 3: "c-1"}
        assign_concurrency_groups([w1, w2, w3], names)
        assert w1.concurrency_group_id == w3.concurrency_group_id
        assert w2.concurrency_group_id != w1.concurrency_group_id

    def test_empty_waves_returns_empty(self):
        result = assign_concurrency_groups([], {})
        assert result == []
