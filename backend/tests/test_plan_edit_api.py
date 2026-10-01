"""HTTP-level tests for manual plan edits (``app.api.plan_edits``).

Covers the contract the plan detail page relies on:

  - GET /api/plans/{id}/vms lists every VM with its wave
  - dry runs report what would move and which rules break, and write nothing
  - move within a plan, into a new wave, into another plan of the partition
  - remove releases the VM to ``available``; add brings one back
  - wave numbers are never renumbered or reused
  - blocking rules (partition mismatch, mapping gap, non-complete plan)
  - overridable rules (wave size, HA family cap) need ``confirm_overrides``
  - edited waves still emit MTV YAML under the same CR name
  - every applied edit is audited
"""

from __future__ import annotations

from tests.test_plans_lifecycle_api import (
    _enroll,
    _mark_blocked,
    _setup_backend,
)

# Distinct name stems → every VM is its own name family, so the HA rule
# stays out of the way unless a test asks for it.
STEMS = [
    "alpha",
    "bravo",
    "charlie",
    "delta",
    "echo",
    "foxtrot",
    "golf",
    "hotel",
    "india",
    "juliet",
    "kilo",
    "lima",
    "mike",
    "november",
]


def _make_plan(client, names: list[str], *, name: str = "Edit Me", **vm_overrides) -> dict:
    vms = [_enroll(client, n, **vm_overrides) for n in names]
    r = client.post("/api/plans", json={"name": name, "vm_ids": [v["id"] for v in vms]})
    assert r.status_code == 202, r.text
    plan = client.get(f"/api/plans/{r.json()['id']}").json()
    assert plan["status"] == "complete", plan
    return plan


def _waves(client, plan_id: int) -> dict[int, list[int]]:
    plan = client.get(f"/api/plans/{plan_id}").json()
    return {w["wave_number"]: list(w["vm_ids"]) for w in plan["waves"]}


def _wave_doc(client, plan_id: int, number: int) -> dict:
    plan = client.get(f"/api/plans/{plan_id}").json()
    return next(w for w in plan["waves"] if w["wave_number"] == number)


def _edit(client, plan_id: int, **body):
    return client.post(f"/api/plans/{plan_id}/edits", json=body)


def _audit_actions(client) -> list[str]:
    body = client.get("/api/audit?limit=200").json()
    rows = body.get("items", body) if isinstance(body, dict) else body
    return [r["action"] for r in rows]


class TestPlanVMs:
    def test_lists_every_vm_with_its_wave(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:12]])
        body = client.get(f"/api/plans/{plan['id']}/vms").json()
        assert body["plan_id"] == plan["id"]
        assert body["editable"] is True
        assert body["total"] == 12
        assert body["limits"]["max_vms_per_wave"] == 10
        assert body["partition"]["target_namespace"] == "prod"
        assert body["partition"]["source_vcenter_name"] == "vc-default"
        by_wave = _waves(client, plan["id"])
        for item in body["items"]:
            assert item["vm_id"] in by_wave[item["wave_number"]]
            assert item["missing"] is False
            assert item["lifecycle_state"] == "planned"
            assert item["baseline_covered"] is None
        assert sorted(w["wave_number"] for w in body["waves"]) == sorted(by_wave)
        assert sum(w["vm_count"] for w in body["waves"]) == 12

    def test_ha_family_only_when_shared(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["billing-db-01", "billing-db-02", "solo-01"])
        items = {i["name"]: i for i in client.get(f"/api/plans/{plan['id']}/vms").json()["items"]}
        assert items["billing-db-01"]["ha_family"] == "billing-db"
        assert items["billing-db-01"]["ha_family_size"] == 2
        assert items["solo-01"]["ha_family"] is None

    def test_unknown_plan_404(self, client):
        assert client.get("/api/plans/9999/vms").status_code == 404

    def test_plan_read_carries_partition(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        assert plan["partition"]["target_namespace"] == "prod"
        listed = client.get("/api/plans").json()
        assert listed[0]["partition"]["target_cluster_name"] == "ocp-default"

    def test_vm_read_names_its_plan_and_wave(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        vid = plan["vm_ids"][0]
        vm = client.get(f"/api/vms/{vid}").json()
        assert vm["plan_id"] == plan["id"]
        assert vm["plan_name"] == "Edit Me"
        assert vm["plan_wave_number"] in _waves(client, plan["id"])
        listed = client.get("/api/vms?lifecycle_state=planned").json()["items"]
        assert {v["plan_id"] for v in listed} == {plan["id"]}
        free = _enroll(client, "free-01")
        assert client.get(f"/api/vms/{free['id']}").json()["plan_id"] is None


class TestMoveWithinPlan:
    def test_move_to_existing_wave(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:12]])
        pid = plan["id"]
        before = _waves(client, pid)
        src, dst = sorted(before)[:2]
        vid = before[src][0]
        # Shrink the destination first so the move itself breaks no rule.
        spare = before[dst][0] if len(before[dst]) >= 10 else None
        if spare is not None:
            r = _edit(client, pid, operation="move_to_wave", vm_ids=[spare], target_wave="new")
            assert r.status_code == 200, r.text
        r = _edit(client, pid, operation="move_to_wave", vm_ids=[vid], target_wave=dst)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] is True and body["dry_run"] is False
        assert body["moves"][0]["from_wave"] == src and body["moves"][0]["to_wave"] == dst
        after = _waves(client, pid)
        assert vid in after[dst] and vid not in after.get(src, [])
        # Plan-level membership is untouched by an in-plan move.
        got = client.get(f"/api/plans/{pid}").json()
        assert sorted(got["vm_ids"]) == sorted(plan["vm_ids"])
        doc = _wave_doc(client, pid, dst)
        assert doc["manual_edit"]["edit_count"] == 1
        assert doc["vm_count"] == len(doc["vm_ids"]) == len(doc["vm_names"])
        assert doc["mtv_yaml_available"] is True

    def test_new_wave_gets_next_number_and_is_never_reused(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:6]])
        pid = plan["id"]
        before = _waves(client, pid)
        top = max(before)
        victim, other = plan["vm_ids"][0], plan["vm_ids"][1]

        r = _edit(client, pid, operation="move_to_wave", vm_ids=[victim], target_wave="new")
        assert r.status_code == 200, r.text
        new_wave = r.json()["plans"][0]["created_waves"][0]
        assert new_wave == top + 1

        # Empty the new wave again: it is dropped ...
        home = next(n for n in before if n != new_wave)
        r = _edit(client, pid, operation="move_to_wave", vm_ids=[victim], target_wave=home)
        assert r.status_code == 200, r.text
        assert r.json()["plans"][0]["removed_waves"] == [new_wave]
        assert new_wave not in _waves(client, pid)

        # ... and its number is not handed to the next new wave.
        r = _edit(client, pid, operation="move_to_wave", vm_ids=[other], target_wave="new")
        assert r.status_code == 200, r.text
        assert r.json()["plans"][0]["created_waves"] == [new_wave + 1]

    def test_existing_wave_numbers_never_change(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:12]])
        pid = plan["id"]
        before = _waves(client, pid)
        first = min(before)
        # Empty the FIRST wave entirely; later waves must keep their numbers.
        r = _edit(
            client,
            pid,
            operation="move_to_wave",
            vm_ids=before[first],
            target_wave="new",
            confirm_overrides=True,
        )
        assert r.status_code == 200, r.text
        after = _waves(client, pid)
        assert first not in after
        for number, ids in before.items():
            if number != first:
                assert after[number] == ids

    def test_move_to_same_wave_is_422(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        waves = _waves(client, plan["id"])
        number = next(iter(waves))
        r = _edit(
            client,
            plan["id"],
            operation="move_to_wave",
            vm_ids=[waves[number][0]],
            target_wave=number,
        )
        assert r.status_code == 422
        assert "already in wave" in r.json()["detail"]

    def test_unknown_wave_and_foreign_vm_are_422(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        stranger = _enroll(client, "stranger-01")
        r = _edit(
            client, plan["id"], operation="move_to_wave", vm_ids=plan["vm_ids"][:1], target_wave=99
        )
        assert r.status_code == 422 and "no wave numbered 99" in r.json()["detail"]
        r = _edit(
            client, plan["id"], operation="move_to_wave", vm_ids=[stranger["id"]], target_wave="new"
        )
        assert r.status_code == 422 and "Not in plan" in r.json()["detail"]
        r = _edit(client, plan["id"], operation="move_to_wave", vm_ids=[424242], target_wave="new")
        assert r.status_code == 404


class TestDryRun:
    def test_dry_run_writes_nothing(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:4]])
        pid = plan["id"]
        before = client.get(f"/api/plans/{pid}").json()
        audits_before = _audit_actions(client)
        r = _edit(
            client,
            pid,
            operation="move_to_wave",
            vm_ids=plan["vm_ids"][:1],
            target_wave="new",
            dry_run=True,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["applied"] is False and body["dry_run"] is True
        assert body["reannotation"] == "none"
        assert len(body["moves"]) == 1
        assert body["plans"][0]["created_waves"]
        assert client.get(f"/api/plans/{pid}").json() == before
        assert _audit_actions(client) == audits_before

    def test_dry_run_reports_blocking_as_200(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01"])
        r = _edit(client, plan["id"], operation="remove", vm_ids=plan["vm_ids"], dry_run=True)
        assert r.status_code == 200
        codes = {v["code"]: v["severity"] for v in r.json()["violations"]}
        assert codes == {"would_empty_plan": "blocking"}
        assert r.json()["requires_confirmation"] is False


class TestOverridableRules:
    def test_wave_size_needs_confirmation(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:12]])
        pid = plan["id"]
        waves = _waves(client, pid)
        big = max(waves, key=lambda n: len(waves[n]))
        small = next(n for n in waves if n != big)
        assert len(waves[big]) == 10
        mover = waves[small][0]

        dry = _edit(
            client, pid, operation="move_to_wave", vm_ids=[mover], target_wave=big, dry_run=True
        ).json()
        size = next(v for v in dry["violations"] if v["code"] == "wave_size")
        assert size["severity"] == "overridable"
        assert size["count"] == 11 and size["limit"] == 10 and size["wave_number"] == big
        assert dry["requires_confirmation"] is True

        r = _edit(client, pid, operation="move_to_wave", vm_ids=[mover], target_wave=big)
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "confirmation_required"
        assert [v["code"] for v in detail["violations"]] == ["wave_size"]
        assert _waves(client, pid) == waves  # refused → unchanged

        r = _edit(
            client,
            pid,
            operation="move_to_wave",
            vm_ids=[mover],
            target_wave=big,
            confirm_overrides=True,
            note="shares a license server with the rest of this wave",
        )
        assert r.status_code == 200, r.text
        doc = _wave_doc(client, pid, big)
        assert len(doc["vm_ids"]) == 11
        assert [o["code"] for o in doc["overrides"]] == ["wave_size"]
        assert doc["overrides"][0]["count"] == 11
        assert doc["manual_edit"]["note"].startswith("shares a license server")
        assert "plan.edit.rule_overridden" in _audit_actions(client)

        # Bringing the wave back under the limit clears the marker.
        r = _edit(client, pid, operation="move_to_wave", vm_ids=[mover], target_wave=small)
        assert r.status_code == 200, r.text
        assert _wave_doc(client, pid, big)["overrides"] == []

    def test_ha_family_cap_needs_confirmation(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        names = ["billing-db-01", "billing-db-02", "billing-db-03", "alpha-01", "bravo-01"]
        plan = _make_plan(client, names)
        pid = plan["id"]
        items = client.get(f"/api/plans/{pid}/vms").json()["items"]
        family = [i for i in items if i["ha_family"] == "billing-db"]
        assert len(family) == 3
        # Put all three in one fresh wave: 3 of 3 where the cap is 2.
        dry = _edit(
            client,
            pid,
            operation="move_to_wave",
            vm_ids=[i["vm_id"] for i in family],
            target_wave="new",
            dry_run=True,
        ).json()
        ha = [v for v in dry["violations"] if v["code"] == "ha_family_cap"]
        assert len(ha) == 1
        assert ha[0]["severity"] == "overridable"
        assert ha[0]["family"] == "billing-db" and ha[0]["count"] == 3 and ha[0]["limit"] == 2
        assert sorted(ha[0]["vm_names"]) == names[:3]

    def test_untouched_generated_plan_reports_nothing(self, client, monkeypatch):
        """An edit that does not worsen a rule the generator itself broke
        must not be blamed for it."""
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["shop-web-01", "shop-web-02", "alpha-01", "bravo-01"])
        pid = plan["id"]
        # Precondition: the generated layout itself holds both members of
        # the pair in one wave (cap 1) — a standing violation.
        by_name = {i["name"]: i for i in client.get(f"/api/plans/{pid}/vms").json()["items"]}
        assert by_name["shop-web-01"]["wave_number"] == by_name["shop-web-02"]["wave_number"]
        alpha = next(
            i
            for i in client.get(f"/api/plans/{pid}/vms").json()["items"]
            if i["name"] == "alpha-01"
        )
        dry = _edit(
            client,
            pid,
            operation="move_to_wave",
            vm_ids=[alpha["vm_id"]],
            target_wave="new",
            dry_run=True,
        ).json()
        assert [v for v in dry["violations"] if v["severity"] != "info"] == []


class TestRemoveAndAdd:
    def test_remove_releases_vm(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:4]])
        pid = plan["id"]
        gone = plan["vm_ids"][0]
        r = _edit(client, pid, operation="remove", vm_ids=[gone])
        assert r.status_code == 200, r.text
        assert r.json()["moves"][0]["to_wave"] is None
        got = client.get(f"/api/plans/{pid}").json()
        assert gone not in got["vm_ids"]
        assert all(gone not in w["vm_ids"] for w in got["waves"])
        vm = client.get(f"/api/vms/{gone}").json()
        assert vm["lifecycle_state"] == "available" and vm["plan_id"] is None
        assert "plan.edit.remove_vm" in _audit_actions(client)

    def test_removing_every_vm_is_blocked(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        r = _edit(client, plan["id"], operation="remove", vm_ids=plan["vm_ids"])
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "blocked"
        assert client.get(f"/api/vms/{plan['vm_ids'][0]}").json()["lifecycle_state"] == "planned"

    def test_add_available_vm(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        pid = plan["id"]
        extra = _enroll(client, "charlie-01")
        wave = min(_waves(client, pid))
        r = _edit(client, pid, operation="add", vm_ids=[extra["id"]], target_wave=wave)
        assert r.status_code == 200, r.text
        got = client.get(f"/api/plans/{pid}").json()
        assert extra["id"] in got["vm_ids"]
        assert extra["id"] in _waves(client, pid)[wave]
        vm = client.get(f"/api/vms/{extra['id']}").json()
        assert vm["lifecycle_state"] == "planned" and vm["plan_id"] == pid
        assert "plan.edit.add_vm" in _audit_actions(client)

    def test_remove_then_add_round_trip(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01", "charlie-01"])
        pid = plan["id"]
        vid = plan["vm_ids"][0]
        assert _edit(client, pid, operation="remove", vm_ids=[vid]).status_code == 200
        r = _edit(client, pid, operation="add", vm_ids=[vid], target_wave="new")
        assert r.status_code == 200, r.text
        assert sorted(client.get(f"/api/plans/{pid}").json()["vm_ids"]) == sorted(plan["vm_ids"])

    def test_add_refuses_vm_in_another_plan(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        a = _make_plan(client, ["alpha-01", "bravo-01"], name="A")
        b = _make_plan(client, ["charlie-01", "delta-01"], name="B")
        r = _edit(client, a["id"], operation="add", vm_ids=b["vm_ids"][:1], target_wave="new")
        assert r.status_code == 422
        codes = [v["code"] for v in r.json()["detail"]["violations"]]
        assert "vm_not_available" in codes

    def test_add_refuses_unmapped_and_blocked_vms(self, client, db_session, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        pid = plan["id"]
        unmapped = _enroll(client, "charlie-01", vsphere_networks=["vlan-999"])
        blocked = _enroll(client, "delta-01")
        _mark_blocked(db_session, blocked["id"])
        dry = _edit(
            client,
            pid,
            operation="add",
            vm_ids=[unmapped["id"], blocked["id"]],
            target_wave="new",
            dry_run=True,
        ).json()
        by_code = {v["code"]: v for v in dry["violations"]}
        assert by_code["mapping_gap"]["severity"] == "blocking"
        assert by_code["mapping_gap"]["vm_ids"] == [unmapped["id"]]
        assert by_code["assessment_blocked"]["vm_ids"] == [blocked["id"]]
        r = _edit(client, pid, operation="add", vm_ids=[unmapped["id"]], target_wave="new")
        assert r.status_code == 422
        assert client.get(f"/api/vms/{unmapped['id']}").json()["lifecycle_state"] == "available"


class TestMoveToPlan:
    def _series(self, client, monkeypatch) -> list[dict]:
        _setup_backend(monkeypatch)
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 3)
        vms = [_enroll(client, f"{s}-01") for s in STEMS[:6]]
        r = client.post("/api/plans", json={"name": "Series", "vm_ids": [v["id"] for v in vms]})
        assert r.status_code == 202, r.text
        assert r.json()["plan_count"] == 2
        return [client.get(f"/api/plans/{p['id']}").json() for p in r.json()["plans"]]

    def test_edit_targets_lists_sibling_batch_first(self, client, monkeypatch):
        first, second = self._series(client, monkeypatch)
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 1000)
        other = _make_plan(client, ["zulu-01", "yankee-01"], name="Unrelated")
        body = client.get(f"/api/plans/{first['id']}/edit-targets").json()
        assert [t["plan_id"] for t in body["targets"]] == [second["id"], other["id"]]
        assert body["targets"][0]["same_series"] is True
        assert body["targets"][1]["same_series"] is False
        assert body["targets"][0]["waves"][0]["vm_count"] >= 1
        assert body["partition"]["target_namespace"] == "prod"

    def test_move_to_sibling_batch(self, client, monkeypatch):
        first, second = self._series(client, monkeypatch)
        vid = first["vm_ids"][0]
        dest = second["waves"][0]["wave_number"]
        r = _edit(
            client,
            first["id"],
            operation="move_to_plan",
            vm_ids=[vid],
            target_plan_id=second["id"],
            target_wave=dest,
            confirm_overrides=True,  # the 3-VM batch size is a plan_size override here
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert {p["plan_id"] for p in body["plans"]} == {first["id"], second["id"]}
        a = client.get(f"/api/plans/{first['id']}").json()
        b = client.get(f"/api/plans/{second['id']}").json()
        assert vid not in a["vm_ids"] and vid in b["vm_ids"]
        assert vid in _waves(client, second["id"])[dest]
        vm = client.get(f"/api/vms/{vid}").json()
        assert vm["lifecycle_state"] == "planned" and vm["plan_id"] == second["id"]
        actions = _audit_actions(client)
        assert actions.count("plan.edit.move_to_plan") == 2
        assert "vm.plan_membership.moved" in actions

    def test_partition_mismatch_is_blocked(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        a = _make_plan(client, ["alpha-01", "bravo-01"], name="A")
        b = _make_plan(
            client, ["charlie-01", "delta-01"], name="B", target_namespace_override="other-ns"
        )
        targets = client.get(f"/api/plans/{a['id']}/edit-targets").json()
        assert targets["targets"] == []
        assert targets["ineligible"][0]["plan_id"] == b["id"]
        assert "other-ns" in targets["ineligible"][0]["reason"]
        r = _edit(
            client,
            a["id"],
            operation="move_to_plan",
            vm_ids=a["vm_ids"][:1],
            target_plan_id=b["id"],
            target_wave="new",
            confirm_overrides=True,
        )
        assert r.status_code == 422
        detail = r.json()["detail"]
        assert detail["code"] == "blocked"
        assert "partition_mismatch" in [v["code"] for v in detail["violations"]]
        assert client.get(f"/api/plans/{a['id']}").json()["vm_ids"] == a["vm_ids"]

    def test_target_must_differ_and_exist(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        a = _make_plan(client, ["alpha-01", "bravo-01"])
        body = {"operation": "move_to_plan", "vm_ids": a["vm_ids"][:1], "target_wave": "new"}
        assert _edit(client, a["id"], **body, target_plan_id=a["id"]).status_code == 422
        assert _edit(client, a["id"], **body, target_plan_id=9999).status_code == 404


class TestPlanStatusGate:
    def test_migrated_plan_is_read_only(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        pid = plan["id"]
        assert client.post(f"/api/plans/{pid}/mark-succeeded").status_code == 200
        assert client.get(f"/api/plans/{pid}/vms").json()["editable"] is False
        r = _edit(
            client, pid, operation="move_to_wave", vm_ids=plan["vm_ids"][:1], target_wave="new"
        )
        assert r.status_code == 409
        assert "only plans in 'complete'" in r.json()["detail"]
        # Dry run says the same thing rather than pretending it could work.
        r = _edit(
            client,
            pid,
            operation="move_to_wave",
            vm_ids=plan["vm_ids"][:1],
            target_wave="new",
            dry_run=True,
        )
        assert r.status_code == 409

    def test_unknown_plan_404(self, client):
        r = _edit(client, 9999, operation="remove", vm_ids=[1])
        assert r.status_code == 404


class TestDownstream:
    def test_edited_wave_yaml_keeps_cr_name(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:4]])
        pid = plan["id"]
        waves = _waves(client, pid)
        number = min(waves)
        extra = _enroll(client, "zulu-01")
        r = _edit(client, pid, operation="add", vm_ids=[extra["id"]], target_wave=number)
        assert r.status_code == 200, r.text
        yaml_text = client.get(f"/api/plans/{pid}/waves/{number}/mtv-yaml").text
        assert f"vv-plan-{pid}-wave-{number}" in yaml_text
        assert "zulu-01" in yaml_text

    def test_baseline_gap_is_surfaced(self, client, db_session, monkeypatch):
        from datetime import datetime, timezone

        from app.models.baseline_run import (
            Baseline,
            BaselineRun,
            BaselineRunStatus,
            VMCollectionStatus,
        )
        from app.models.ssh_key import SSHKey

        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        pid = plan["id"]
        waves = _waves(client, pid)
        number = min(waves)
        # No client calls between flush and commit: the test engine is a
        # single shared connection, and a request's session would roll
        # the pending rows back.
        key = SSHKey(
            name="k",
            public_key="ssh-ed25519 AAAA",
            private_key_path="/app/keys/k",
            fingerprint="fp",
        )
        db_session.add(key)
        db_session.flush()
        run = BaselineRun(
            plan_id=pid,
            wave_number=number,
            ssh_key_id=key.id,
            status=BaselineRunStatus.completed,
            total_vms=2,
            captured_vms=2,
            completed_at=datetime.now(timezone.utc),
        )
        db_session.add(run)
        db_session.flush()
        for vid in waves[number]:
            db_session.add(
                Baseline(baseline_run_id=run.id, vm_id=vid, status=VMCollectionStatus.captured)
            )
        db_session.commit()

        extra = _enroll(client, "charlie-01")
        dry = _edit(
            client, pid, operation="add", vm_ids=[extra["id"]], target_wave=number, dry_run=True
        ).json()
        info = [v for v in dry["violations"] if v["code"] == "baseline_not_covered"]
        assert info and info[0]["severity"] == "info"
        assert dry["requires_confirmation"] is False

        assert (
            _edit(
                client, pid, operation="add", vm_ids=[extra["id"]], target_wave=number
            ).status_code
            == 200
        )
        state = client.get(f"/api/plans/{pid}/vms").json()
        wave = next(w for w in state["waves"] if w["wave_number"] == number)
        assert wave["baseline"]["missing_vm_ids"] == [extra["id"]]
        rows = {i["vm_id"]: i for i in state["items"]}
        assert rows[extra["id"]]["baseline_covered"] is False
        preview = client.get(f"/api/plans/{pid}/waves/{number}/preview").json()
        assert [g["vm_id"] for g in preview["baseline_missing"]] == [extra["id"]]

    def test_running_baseline_blocks_edit(self, client, db_session, monkeypatch):
        from app.models.baseline_run import BaselineRun, BaselineRunStatus
        from app.models.ssh_key import SSHKey

        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01", "charlie-01"])
        pid = plan["id"]
        waves = _waves(client, pid)
        number = min(waves)
        key = SSHKey(
            name="k",
            public_key="ssh-ed25519 AAAA",
            private_key_path="/app/keys/k",
            fingerprint="fp",
        )
        db_session.add(key)
        db_session.flush()
        db_session.add(
            BaselineRun(
                plan_id=pid,
                wave_number=number,
                ssh_key_id=key.id,
                status=BaselineRunStatus.running,
                total_vms=1,
            )
        )
        db_session.commit()
        r = _edit(
            client, pid, operation="move_to_wave", vm_ids=waves[number][:1], target_wave="new"
        )
        assert r.status_code == 422
        assert "wave_run_in_progress" in [v["code"] for v in r.json()["detail"]["violations"]]


class TestReannotation:
    def test_edit_ends_with_llm_rationale(self, client, monkeypatch):
        """TestClient runs the background task inline, so by the next GET
        the interim text has been replaced by the (mock) LLM's."""
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:4]])
        pid = plan["id"]
        r = _edit(
            client,
            pid,
            operation="move_to_wave",
            vm_ids=plan["vm_ids"][:1],
            target_wave="new",
            note="moves with the finance cutover",
        )
        assert r.status_code == 200, r.text
        assert r.json()["reannotation"] == "queued"
        new_wave = r.json()["plans"][0]["created_waves"][0]
        doc = _wave_doc(client, pid, new_wave)
        assert doc["method"] == "llm"
        assert doc["reannotation"]["state"] == "done"
        assert doc["rationale"] == doc["description"]
        assert "plan.edit.reannotated" in _audit_actions(client)
        logs = client.get("/api/inference-logs?limit=50").json()
        rows = logs.get("items", logs) if isinstance(logs, dict) else logs
        prompts = [str(row.get("input_messages")) for row in rows]
        assert any("OPERATOR CONTEXT" in p and "finance cutover" in p for p in prompts)

    def test_no_backend_keeps_deterministic_text(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, [f"{s}-01" for s in STEMS[:4]])
        pid = plan["id"]

        def _boom():
            raise RuntimeError("backend unavailable")

        monkeypatch.setattr("app.core.llm.runtime.get_active_backend", _boom)
        r = _edit(
            client, pid, operation="move_to_wave", vm_ids=plan["vm_ids"][:1], target_wave="new"
        )
        assert r.status_code == 200, r.text
        doc = _wave_doc(client, pid, r.json()["plans"][0]["created_waves"][0])
        assert doc["method"] == "mechanical_fallback"
        assert "arranged this wave's membership by hand" in doc["description"]
        assert "HA families are spread" not in doc["description"]

    def test_reannotate_endpoint(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        pid = plan["id"]
        number = min(_waves(client, pid))
        r = client.post(f"/api/plans/{pid}/waves/{number}/reannotate")
        assert r.status_code == 202, r.text
        assert _wave_doc(client, pid, number)["reannotation"]["state"] == "done"
        assert client.post(f"/api/plans/{pid}/waves/99/reannotate").status_code == 404

    def test_startup_sweep_abandons_queued(self, client, db_session, monkeypatch):
        from sqlalchemy.orm.attributes import flag_modified

        from app.core.plan_reannotation import abandon_stale_reannotations
        from app.models.plan import MigrationPlan

        _setup_backend(monkeypatch)
        plan = _make_plan(client, ["alpha-01", "bravo-01"])
        row = db_session.get(MigrationPlan, plan["id"])
        waves = [dict(w) for w in row.waves]
        waves[0]["method"] = "mechanical_interim"
        waves[0]["reannotation"] = {"state": "queued", "token": "t"}
        row.waves = waves
        flag_modified(row, "waves")
        db_session.commit()
        assert abandon_stale_reannotations() == 1
        doc = client.get(f"/api/plans/{plan['id']}").json()["waves"][0]
        assert doc["reannotation"]["state"] == "abandoned"
        assert doc["method"] == "mechanical_fallback"
        assert abandon_stale_reannotations() == 0
