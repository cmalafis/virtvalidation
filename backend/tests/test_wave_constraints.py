"""Planner-rule checks for a hand-edited wave layout (pure Python)."""

from __future__ import annotations

from app.core.wave_constraints import (
    OVERRIDABLE,
    check_waves,
    diff_violations,
    ha_family_violations,
    wave_size_violations,
)


def _members(*names: str, start: int = 1) -> list[tuple[int, str]]:
    return [(start + i, n) for i, n in enumerate(names)]


class TestWaveSize:
    def test_at_limit_is_fine(self):
        waves = {1: _members(*[f"app{i}-01" for i in range(10)])}
        assert wave_size_violations(waves) == []

    def test_over_limit_reports_count_and_limit(self):
        waves = {3: _members(*[f"app{i}-01" for i in range(12)])}
        [v] = wave_size_violations(waves, plan_id=7)
        assert (v.code, v.severity) == ("wave_size", OVERRIDABLE)
        assert (v.plan_id, v.wave_number, v.count, v.limit) == (7, 3, 12, 10)
        assert "Wave 3" in v.message


class TestHAFamily:
    def test_singletons_never_violate(self):
        waves = {1: _members("alpha-01", "bravo-01", "charlie-01")}
        assert ha_family_violations(waves) == []

    def test_pair_in_one_wave_violates(self):
        # family_cap(2) == 1: both members of a pair together is over.
        waves = {1: _members("ad-dc-01", "ad-dc-02")}
        [v] = ha_family_violations(waves)
        assert (v.code, v.family, v.count, v.limit) == ("ha_family_cap", "ad-dc", 2, 1)
        assert v.vm_names == ("ad-dc-01", "ad-dc-02")

    def test_cap_follows_plan_wide_family_size(self):
        # Three members plan-wide → cap 2. Two together is allowed ...
        ok = {1: _members("billing-db-01", "billing-db-02"), 2: _members("billing-db-03", start=9)}
        assert ha_family_violations(ok) == []
        # ... three together is not.
        bad = {1: _members("billing-db-01", "billing-db-02", "billing-db-03")}
        [v] = ha_family_violations(bad)
        assert (v.count, v.limit) == (3, 2)

    def test_removing_a_member_tightens_the_cap_elsewhere(self):
        before = {1: _members("pay-db-01", "pay-db-02"), 2: _members("pay-db-03", start=9)}
        after = {
            1: _members("pay-db-01", "pay-db-02")
        }  # pay-db-03 left the plan → family of 2, cap 1
        assert ha_family_violations(before) == []
        [v] = ha_family_violations(after)
        assert v.wave_number == 1 and v.limit == 1


class TestDiff:
    def test_standing_violation_is_not_reintroduced(self):
        layout = {1: _members("shop-web-01", "shop-web-02")}
        standing = check_waves(layout)
        assert standing  # the generator's own layout already breaks the cap
        assert diff_violations(standing, check_waves(layout)) == []

    def test_worsened_violation_is_reported(self):
        before = check_waves({1: _members(*[f"a{i}-01" for i in range(11)])})
        after = check_waves({1: _members(*[f"a{i}-01" for i in range(12)])})
        [v] = diff_violations(before, after)
        assert v.count == 12

    def test_new_violation_is_reported(self):
        before = check_waves({1: _members("pay-db-01"), 2: _members("pay-db-02", start=5)})
        after = check_waves({1: _members("pay-db-01", "pay-db-02")})
        assert [v.code for v in diff_violations(before, after)] == ["ha_family_cap"]

    def test_to_dict_is_json_shaped(self):
        [v] = wave_size_violations({1: _members(*[f"a{i}-01" for i in range(11)])})
        d = v.to_dict()
        assert d["vm_ids"] == [] and d["severity"] == "overridable" and d["count"] == 11
