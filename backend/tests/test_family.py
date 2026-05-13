"""Family detection + overconcentration split unit tests.

Pins every screenshot example from the prompt + edge cases. If this
file goes red, the planner's HA anti-affinity rule is broken before
the wave packer even sees the groups.
"""

from __future__ import annotations

import pytest

from app.core.family import (
    detect_family,
    family_cap,
    split_into_families,
    split_overconcentrated_families,
)


class TestDetectFamily:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("AD-DC-01", "ad-dc"),
            ("AD-DC-02", "ad-dc"),
            ("ad-dc-99", "ad-dc"),
            ("billing-app-037", "billing-app"),
            ("billing-db-014", "billing-db"),
            ("billing-db-036", "billing-db"),
            ("billing-db-043", "billing-db"),
            ("backup-s-app-013", "backup-s-app"),
            ("postgres-replica-01", "postgres-replica"),
            ("lonewolf-server-99", "lonewolf-server"),
            ("noname", "noname"),
            ("svc_db_03", "svc-db"),  # underscore separator
        ],
    )
    def test_screenshot_examples(self, name, expected):
        assert detect_family(name) == expected

    def test_empty(self):
        assert detect_family("") == ""

    def test_all_digits(self):
        # All-digit name collapses to nothing; fall back to original.
        assert detect_family("01-02-03") == "01-02-03"

    def test_only_role_suffix(self):
        # If the whole name is a role suffix, no anti-affinity treatment.
        assert detect_family("DC") == "dc"

    def test_billing_db_three_members(self):
        names = ["billing-db-014", "billing-db-036", "billing-db-043"]
        families = split_into_families(names)
        assert families == {"billing-db": names}

    def test_split_billing_keeps_app_and_db_separate(self):
        names = ["billing-app-001", "billing-db-001", "billing-app-002"]
        families = split_into_families(names)
        assert set(families.keys()) == {"billing-app", "billing-db"}
        assert families["billing-app"] == ["billing-app-001", "billing-app-002"]
        assert families["billing-db"] == ["billing-db-001"]


class TestFamilyCap:
    @pytest.mark.parametrize(
        "size,cap",
        [(1, 1), (2, 1), (3, 2), (4, 2), (5, 3), (6, 3), (11, 6)],
    )
    def test_cap_values(self, size, cap):
        assert family_cap(size) == cap


# ---------------------------------------------------------------------------
# Stage-3 split tests — these exercise the integration with VMGroup
# ---------------------------------------------------------------------------
class TestSplitOverconcentrated:
    @staticmethod
    def _group(vm_ids: list[int], discriminator: str = "test"):
        from app.core.preclassifier import GroupKey, VMGroup

        key = GroupKey(
            vcenter_id=1,
            target_namespace="ns",
            role="data",
            state="stateful",
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
            estimated_role="data",
            estimated_state="stateful",
            migration_risk="high",
            dependency_hints=[],
            notes="seed",
            ha_members=[],
        )

    def test_no_split_when_within_cap(self):
        # 2 billing-db VMs in one group — cap is ceil(2/2) = 1.
        # That means group has 2 members of a family with cap 1: SPLIT required.
        # So this exact case actually SHOULD split. Use a singleton case instead.
        group = self._group([1])
        out = split_overconcentrated_families([group], {1: "lonewolf-99"})
        assert len(out) == 1
        assert out[0] is group

    def test_three_member_family_in_one_group_splits(self):
        group = self._group([10, 20, 30])
        names = {10: "billing-db-001", 20: "billing-db-002", 30: "billing-db-003"}
        # Global family size 3 → cap 2 per group. The one group has 3
        # members → must split into ≥2 sub-groups.
        out = split_overconcentrated_families([group], names)
        assert len(out) >= 2
        per_group_counts = [len(g.vm_ids) for g in out]
        # Each sub-group has ≤2 of the billing-db family.
        assert all(c <= 2 for c in per_group_counts)
        # All original VM ids preserved.
        all_ids = [vid for g in out for vid in g.vm_ids]
        assert sorted(all_ids) == [10, 20, 30]

    def test_four_members_split_into_two_buckets(self):
        group = self._group([1, 2, 3, 4])
        names = {
            1: "ad-dc-01",
            2: "ad-dc-02",
            3: "ad-dc-03",
            4: "ad-dc-04",
        }
        # Global family size 4 → cap 2 per group. So splits into 2
        # buckets of 2 each.
        out = split_overconcentrated_families([group], names)
        assert len(out) == 2
        assert sorted(len(g.vm_ids) for g in out) == [2, 2]

    def test_mixed_families_keep_per_family_cap(self):
        # Two families: 3 db + 2 web in ONE group.
        # db cap = ceil(3/2) = 2; web cap = ceil(2/2) = 1.
        # So bucket A: 2 db + 1 web; bucket B: 1 db + 1 web.
        group = self._group([1, 2, 3, 4, 5])
        names = {
            1: "billing-db-01",
            2: "billing-db-02",
            3: "billing-db-03",
            4: "billing-web-01",
            5: "billing-web-02",
        }
        out = split_overconcentrated_families([group], names)
        # Every bucket has ≤2 db members and ≤1 web member.
        from app.core.family import detect_family

        for g in out:
            db = sum(1 for vid in g.vm_ids if detect_family(names[vid]) == "billing-db")
            web = sum(1 for vid in g.vm_ids if detect_family(names[vid]) == "billing-web")
            assert db <= 2
            assert web <= 1
        all_ids = sorted(vid for g in out for vid in g.vm_ids)
        assert all_ids == [1, 2, 3, 4, 5]

    def test_singleton_family_no_split(self):
        # 10 VMs, every one a distinct family — no anti-affinity treatment.
        ids = list(range(1, 11))
        names = {i: f"app-{i}" for i in ids}
        group = self._group(ids)
        out = split_overconcentrated_families([group], names)
        assert len(out) == 1
        assert out[0].vm_ids == sorted(ids)
