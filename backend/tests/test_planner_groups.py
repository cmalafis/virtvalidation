"""Integration tests for the group-based planner path.

Drives the planner end-to-end through MockBackend so the test is fast
and deterministic. Pins the contract that the LLM never sees raw vm_ids
(it sees group_ids), that mechanical expansion guarantees every input
vm_id lands in exactly one wave, and that the API surfaces both groups
and waves transparently.
"""

from __future__ import annotations

import time

import pytest

from app.core.llm.mock_backend import MockBackend
from app.core.planner import MigrationPlanner, PlannerError
from app.models.vm import VM


def _vm(
    id_: int,
    name: str,
    *,
    vcenter: int | None = 1,
    target_namespace: str = "prod",
    networks: list[str] | None = None,
    datastores: list[str] | None = None,
    application_hint: str | None = None,
    os_family: str = "rhel",
) -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.local",
        source_vcenter_id=vcenter,
        target_namespace=target_namespace,
        vsphere_networks=list(networks or []),
        vsphere_datastores=list(datastores or []),
        application_hint=application_hint,
        os_family=os_family,
    )
    vm.id = id_
    return vm


# ---------------------------------------------------------------------------
# Two-stage flow
# ---------------------------------------------------------------------------
def test_plan_with_groups_places_every_vm_in_exactly_one_wave():
    vms = [
        _vm(i, f"web-prod-{i:02d}", networks=["web-net"], application_hint="epic")
        for i in range(1, 11)
    ] + [
        _vm(20 + i, f"postgres-db-{i:02d}", networks=["db-net"], application_hint="epic")
        for i in range(1, 4)
    ]
    planner = MigrationPlanner(backend=MockBackend())
    result = planner.plan_with_groups(vms)

    placed: list[int] = []
    for wave in result["waves"]:
        placed.extend(wave["vm_ids"])
    assert sorted(placed) == sorted(vm.id for vm in vms)
    # No duplicates across waves.
    assert len(placed) == len(set(placed))


def test_plan_with_groups_returns_groups_and_count_for_api():
    vms = [_vm(i, f"web-{i:02d}", networks=["w"]) for i in range(1, 6)]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    assert result["groups_formed"] >= 1
    assert result["groups_formed"] == len(result["groups"])
    for g in result["groups"]:
        assert "wave_number" in g  # stamped during expansion


def test_plan_with_groups_57_vm_fleet_succeeds():
    # The customer fleet that previously failed at planner integrity
    # checks (LLM dropped vm_ids). With pre-classification the LLM sees
    # only ~5-10 groups, so the failure mode is gone.
    vms = []
    idx = 1
    for i in range(1, 16):
        vms.append(_vm(idx, f"epic-web-{i:02d}", networks=["epic-web"], application_hint="epic-emr"))
        idx += 1
    for i in range(1, 11):
        vms.append(_vm(idx, f"epic-app-{i:02d}", networks=["epic-app"], application_hint="epic-emr"))
        idx += 1
    for i in range(1, 5):
        vms.append(_vm(idx, f"postgres-db-{i:02d}", networks=["epic-db"], application_hint="epic-emr"))
        idx += 1
    for i in range(1, 13):
        vms.append(_vm(idx, f"analytics-worker-{i:02d}", vcenter=2, networks=["analytics"], application_hint="analytics"))
        idx += 1
    for i in range(1, 6):
        vms.append(_vm(idx, f"dc-ldap-{i:02d}", vcenter=2, application_hint="infra"))
        idx += 1
    for i in range(1, 7):
        vms.append(_vm(idx, f"payroll-app-{i:02d}", application_hint="payroll"))
        idx += 1

    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    placed: list[int] = []
    for wave in result["waves"]:
        placed.extend(wave["vm_ids"])
    assert sorted(placed) == sorted(vm.id for vm in vms)


def test_plan_with_groups_empty_input_raises():
    with pytest.raises(PlannerError):
        MigrationPlanner(backend=MockBackend()).plan_with_groups([])


def test_plan_with_groups_waves_carry_group_ids():
    vms = [_vm(i, f"web-{i:02d}", networks=["w"]) for i in range(1, 4)]
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    for wave in result["waves"]:
        assert "group_ids" in wave
        assert wave["group_ids"]


# ---------------------------------------------------------------------------
# API integration
# ---------------------------------------------------------------------------
def _seed_vms(client, count=5, prefix="web"):
    for i in range(1, count + 1):
        client.post("/api/vms", json={
            "name": f"{prefix}-{i:02d}",
            "source_hostname": f"{prefix}-{i:02d}.local",
            "vsphere_networks": [f"{prefix}-net"],
            "application_hint": "test-app",
        }).raise_for_status()
    rows = client.get("/api/vms").json()["items"]
    return [r["id"] for r in rows]


def test_post_plans_uses_preclassification_by_default(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.llm_backend_type", "mock")
    from app.core.llm.factory import reset_backend_cache
    reset_backend_cache()
    vm_ids = _seed_vms(client, count=6)
    r = client.post("/api/plans", json={"vm_ids": vm_ids})
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["groups_formed"] >= 1
    assert body["groups"]
    # Plan invariant: every input vm_id appears in waves.
    placed = sorted(vid for wave in body["waves"] for vid in wave["vm_ids"])
    assert placed == sorted(vm_ids)


def test_post_plans_preclassification_disabled_falls_back_to_raw(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.llm_backend_type", "mock")
    from app.core.llm.factory import reset_backend_cache
    reset_backend_cache()
    vm_ids = _seed_vms(client, count=3)
    r = client.post("/api/plans", json={
        "vm_ids": vm_ids,
        "preclassification_enabled": False,
    })
    assert r.status_code == 201, r.json()
    body = r.json()
    # Legacy path doesn't populate groups.
    assert body["groups_formed"] == 0
    assert body["groups"] == []


def test_post_preview_groups_returns_groups_without_persisting(client, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.llm_backend_type", "mock")
    from app.core.llm.factory import reset_backend_cache
    reset_backend_cache()
    vm_ids = _seed_vms(client, count=6)
    r = client.post("/api/plans/preview-groups", json={"vm_ids": vm_ids})
    assert r.status_code == 200
    body = r.json()
    assert body["vm_count"] == 6
    assert body["groups_formed"] >= 1
    assert body["groups"]
    # No plan was persisted.
    assert client.get("/api/plans").json() == []


def test_post_preview_groups_404_for_unknown_vm_ids(client):
    r = client.post("/api/plans/preview-groups", json={"vm_ids": [99999]})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Performance: LLM stays cheap regardless of VM count
# ---------------------------------------------------------------------------
def test_plan_with_groups_10_vms_under_a_second():
    vms = [_vm(i, f"web-{i:02d}", networks=["w"]) for i in range(1, 11)]
    started = time.monotonic()
    MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"10-VM plan took {elapsed:.2f}s; expected <1s"


def test_plan_with_groups_100_vms_under_two_seconds():
    vms = []
    for i in range(1, 101):
        role = ["web", "app", "db", "edge"][i % 4]
        vms.append(_vm(
            i, f"{role}-{i:03d}",
            networks=[f"net-{i % 5}"],
            application_hint=f"app-{i % 10}",
        ))
    started = time.monotonic()
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, f"100-VM plan took {elapsed:.2f}s; expected <2s"
    placed = sorted(vid for wave in result["waves"] for vid in wave["vm_ids"])
    assert placed == sorted(vm.id for vm in vms)


def test_plan_with_groups_1000_vms_under_ten_seconds():
    # The key insight: LLM time stays roughly constant because the
    # preclassifier caps group count at llm_max_items_per_call.
    vms = []
    for i in range(1, 1001):
        role = ["web", "app", "db", "edge"][i % 4]
        vms.append(_vm(
            i, f"{role}-{i:04d}",
            networks=[f"net-{i % 8}"],
            datastores=[f"ds-{i % 4}"],
            application_hint=f"app-{i % 25}",
        ))
    started = time.monotonic()
    result = MigrationPlanner(backend=MockBackend()).plan_with_groups(vms)
    elapsed = time.monotonic() - started
    assert elapsed < 10.0, f"1000-VM plan took {elapsed:.2f}s; expected <10s"
    placed = sorted(vid for wave in result["waves"] for vid in wave["vm_ids"])
    assert placed == sorted(vm.id for vm in vms)
