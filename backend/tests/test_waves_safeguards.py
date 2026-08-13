"""Tests for the production safeguards on wave runs:

- global SSH kill-switch (503),
- authorization gate for production / classified hosts (422),
- dry-run preview (no SSH).
"""

from __future__ import annotations

import pytest

from app.core import config as cfg_mod
from app.models.plan import MigrationPlan
from app.models.settings import AppSettings
from app.models.vcenter import ClassificationLevel, VCenterSource
from app.models.vm import VM
from app.services import ssh_key_service


@pytest.fixture
def isolated_key_dir(tmp_path, monkeypatch):
    target = tmp_path / "keys" / "id_ed25519"
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_path", str(target))
    monkeypatch.setattr(cfg_mod.settings, "fips_mode", False)
    return tmp_path / "keys"


def _make_plan(db, vms) -> MigrationPlan:
    plan = MigrationPlan(
        name="p",
        vm_ids=[v.id for v in vms],
        waves=[
            {"wave_number": 1, "vm_ids": [v.id for v in vms], "vm_names": [v.name for v in vms]}
        ],
        model="",
        status="complete",
    )
    db.add(plan)
    db.commit()
    return plan


@pytest.fixture
def dev_plan(db_session):
    """A non-production plan that does NOT trip the authorization gate."""
    vms = [
        VM(
            name=f"dev-{i}",
            source_hostname=f"d{i}",
            ip_address=f"10.0.0.{i}",
            environment="development",
        )
        for i in range(2)
    ]
    db_session.add_all(vms)
    db_session.flush()
    plan = _make_plan(db_session, vms)
    return plan, vms


@pytest.fixture
def prod_plan(db_session):
    """A production plan that DOES require authorization."""
    vms = [
        VM(
            name=f"prod-{i}",
            source_hostname=f"p{i}",
            ip_address=f"10.0.1.{i}",
            environment="production",
        )
        for i in range(2)
    ]
    db_session.add_all(vms)
    db_session.flush()
    plan = _make_plan(db_session, vms)
    return plan, vms


def _key(db) -> int:
    return ssh_key_service.generate_keypair(db, name="k").id


# ---------------------------------------------------------------------------
# Kill-switch
# ---------------------------------------------------------------------------
def test_kill_switch_blocks_baseline_with_503(isolated_key_dir, client, dev_plan, db_session):
    plan, _ = dev_plan
    key_id = _key(db_session)
    # Flip the kill-switch off.
    db_session.add(AppSettings(id=1, ssh_operations_enabled=False))
    db_session.commit()

    r = client.post(f"/api/plans/{plan.id}/waves/1/baseline", json={"ssh_key_id": key_id})
    assert r.status_code == 503
    assert "kill-switch" in r.json()["detail"].lower()


def test_kill_switch_off_allows_run(isolated_key_dir, client, dev_plan, db_session, monkeypatch):
    plan, _ = dev_plan
    key_id = _key(db_session)
    db_session.add(AppSettings(id=1, ssh_operations_enabled=True))
    db_session.commit()

    # Stub the background task so we don't actually SSH.
    monkeypatch.setattr("app.api.waves.run_baseline_task", lambda *a, **k: None)
    r = client.post(f"/api/plans/{plan.id}/waves/1/baseline", json={"ssh_key_id": key_id})
    assert r.status_code == 202


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------
def test_production_wave_requires_authorization(isolated_key_dir, client, prod_plan, db_session):
    plan, _ = prod_plan
    key_id = _key(db_session)
    r = client.post(f"/api/plans/{plan.id}/waves/1/baseline", json={"ssh_key_id": key_id})
    assert r.status_code == 422
    assert "authorization required" in r.json()["detail"].lower()
    assert "production" in r.json()["detail"].lower()


def test_production_wave_proceeds_with_authorization(
    isolated_key_dir, client, prod_plan, db_session, monkeypatch
):
    plan, _ = prod_plan
    key_id = _key(db_session)
    monkeypatch.setattr("app.api.waves.run_baseline_task", lambda *a, **k: None)
    r = client.post(
        f"/api/plans/{plan.id}/waves/1/baseline",
        json={
            "ssh_key_id": key_id,
            "authorized_by": "ops@example.gov",
            "authorization_reason": "Approved cutover window CR-1234",
        },
    )
    assert r.status_code == 202
    run_id = r.json()["baseline_run_id"]
    from app.models.baseline_run import BaselineRun

    run = db_session.get(BaselineRun, run_id)
    assert run.authorized_by == "ops@example.gov"
    assert "CR-1234" in run.authorization_reason


def test_classified_vcenter_requires_authorization(isolated_key_dir, client, db_session):
    vc = VCenterSource(
        name="cls", hostname="vc.classified", classification_level=ClassificationLevel.cui
    )
    db_session.add(vc)
    db_session.flush()
    vms = [
        VM(
            name="cui-0",
            source_hostname="c0",
            ip_address="10.0.2.0",
            environment="development",
            source_vcenter_id=vc.id,
        )
    ]
    db_session.add_all(vms)
    db_session.flush()
    plan = _make_plan(db_session, vms)
    key_id = _key(db_session)

    r = client.post(f"/api/plans/{plan.id}/waves/1/baseline", json={"ssh_key_id": key_id})
    assert r.status_code == 422
    assert "cui" in r.json()["detail"].lower()


def test_dev_wave_needs_no_authorization(
    isolated_key_dir, client, dev_plan, db_session, monkeypatch
):
    plan, _ = dev_plan
    key_id = _key(db_session)
    monkeypatch.setattr("app.api.waves.run_baseline_task", lambda *a, **k: None)
    r = client.post(f"/api/plans/{plan.id}/waves/1/baseline", json={"ssh_key_id": key_id})
    assert r.status_code == 202


# ---------------------------------------------------------------------------
# Dry-run preview
# ---------------------------------------------------------------------------
def test_preview_lists_hosts_and_commands_without_connecting(client, prod_plan, db_session):
    plan, vms = prod_plan
    r = client.get(f"/api/plans/{plan.id}/waves/1/preview")
    assert r.status_code == 200
    body = r.json()
    assert body["vm_count"] == 2
    assert body["requires_authorization"] is True
    assert "production" in body["authorization_reason"].lower()
    assert body["ssh_operations_enabled"] is True
    first = body["vms"][0]
    assert first["host"]
    # Read-only commands only — every preview command must be a known probe.
    assert any("os-release" in c for c in first["commands"])
    assert all("rm " not in c and "Set-" not in c for c in first["commands"])
