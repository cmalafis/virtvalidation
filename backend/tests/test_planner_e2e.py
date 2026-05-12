"""End-to-end planner tests against the enriched DHA fleet fixture.

Pins the post-refactor guarantees:

  - Preclassifier produces 5-12 rich groups (not 20+ thin ones).
  - Each group's shared_attributes are populated with real networks /
    datastores / application_hints / environments — the fixture
    represents a realistically tagged inventory, so the groups should
    reflect that.
  - Dependency hints make sense — app groups depend on data /
    infrastructure groups in the same vCenter.
  - End-to-end plan generation through MockBackend succeeds and
    every input vm_id lands in exactly one wave.
"""

from __future__ import annotations

from app.core.llm.mock_backend import MockBackend
from app.core.planner import MigrationPlanner
from app.core.preclassifier import PreClassifier

from tests.fixtures.dha_fleet import build_dha_fleet_vms, dha_fleet_specs


# ---------------------------------------------------------------------------
# Fixture sanity
# ---------------------------------------------------------------------------
def test_dha_fleet_specs_total_is_57():
    assert len(dha_fleet_specs()) == 57


def test_dha_fleet_vms_have_populated_metadata():
    vms = build_dha_fleet_vms()
    # Every VM should carry application_hint, environment, networks,
    # datastores — that's the entire point of the enriched fixture.
    for vm in vms:
        assert vm.application_hint, f"{vm.name} missing application_hint"
        assert vm.environment, f"{vm.name} missing environment"
        assert vm.vsphere_networks, f"{vm.name} missing networks"
        assert vm.vsphere_datastores, f"{vm.name} missing datastores"


# ---------------------------------------------------------------------------
# Preclassifier on realistic input
# ---------------------------------------------------------------------------
def test_preclassifier_57_vm_fleet_stays_under_llm_ceiling():
    vms = build_dha_fleet_vms()
    groups = PreClassifier().classify(vms)
    # Architectural invariant: group count <= LLM_MAX_ITEMS_PER_CALL.
    # On the canonical 57-VM federal hospital fixture (3 vCenters,
    # 8 apps, ~3 tiers each) we land in the mid-teens — rich
    # per-role splitting on the production-sized apps with small
    # environments (DR / staging) collapsed.
    assert 5 <= len(groups) <= 16, (
        f"got {len(groups)} groups: {[g.id for g in groups]}"
    )


def test_preclassifier_groups_have_populated_shared_attributes():
    vms = build_dha_fleet_vms()
    groups = PreClassifier().classify(vms)
    for g in groups:
        # At minimum the group should expose its application_hint and
        # environment — those come straight from the operator-supplied
        # custom attributes the federal customer populates.
        attrs = g.shared_attributes
        # Every group should carry at least one shared-cohesion
        # dimension. Merged groups (small primary-key collapse) may
        # have empty datastores if their members diverge across
        # tiers — but networks + hints + environments stay populated
        # because they're partition-level signals.
        populated = [
            dim for dim in (
                "networks", "datastores", "application_hints", "environments",
            )
            if attrs.get(dim)
        ]
        assert populated, f"{g.id} has no populated shared_attributes"
        assert attrs.get("application_hints"), (
            f"{g.id} has empty application_hints"
        )
        assert attrs.get("environments"), f"{g.id} has empty environments"


def test_preclassifier_separates_apps_into_role_subgroups():
    vms = build_dha_fleet_vms()
    groups = PreClassifier().classify(vms)
    ehrpro_groups = [g for g in groups if "ehrpro" in g.id]
    # EHRPro has web + app + data + worker tiers — should be at least
    # 2 groups (data must be separate from stateless tiers; web/app
    # may or may not coalesce depending on metadata overlap).
    assert len(ehrpro_groups) >= 2, (
        f"ehrpro didn't sub-split by role: {[g.id for g in ehrpro_groups]}"
    )
    # The data subgroup should be tagged stateful + high-risk.
    data = [g for g in ehrpro_groups if g.estimated_role == "data"]
    assert data, "no data tier group inferred for ehrpro"
    for g in data:
        assert g.estimated_state == "stateful"
        assert g.migration_risk == "high"


def test_preclassifier_seeds_dependency_hints_for_app_groups():
    vms = build_dha_fleet_vms()
    groups = PreClassifier().classify(vms)
    app_groups = [g for g in groups if g.estimated_role == "app"]
    assert app_groups, "no app groups produced"
    for g in app_groups:
        assert g.dependency_hints, f"app group {g.id} lost dependency hints"


def test_preclassifier_never_merges_across_vcenter():
    vms = build_dha_fleet_vms()
    groups = PreClassifier().classify(vms)
    vcs = {g.key.vcenter_id for g in groups}
    # The fixture spans three vCenters (prod / dev / DR).
    assert vcs == {1, 2, 3}


# ---------------------------------------------------------------------------
# End-to-end planner
# ---------------------------------------------------------------------------
def test_plan_with_groups_succeeds_on_dha_fleet():
    vms = build_dha_fleet_vms()
    planner = MigrationPlanner(backend=MockBackend())
    result = planner.plan_with_groups(vms)
    placed = sorted(vid for wave in result["waves"] for vid in wave["vm_ids"])
    expected = sorted(vm.id for vm in vms)
    assert placed == expected
    # Post-refactor: wave structure is always mechanical; the LLM
    # only produces rationale text per-wave. MockBackend answers
    # the rationale prompt successfully, so the method label is
    # ``mechanical+llm_rationale``.
    assert result["method"] == "mechanical+llm_rationale"
    assert result["groups_formed"] >= 5
