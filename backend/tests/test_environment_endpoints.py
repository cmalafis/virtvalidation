"""Tests for the environment management API endpoints.

Three endpoints:

  - PATCH /api/vms/{id}/environment — single VM override.
  - POST  /api/vms/bulk-set-environment — atomic multi-VM override.
  - POST  /api/vms/redetect-environment — bulk re-run the cascade.

Federal customer audit trails record each operation; tests pin the
``vm.environment.*`` actions in the audit log.
"""

from __future__ import annotations


def _create(client, **overrides):
    body = {"name": "vm-default", "source_hostname": "vm-default.local"}
    body.update(overrides)
    r = client.post("/api/vms", json=body)
    assert r.status_code == 201, r.json()
    return r.json()


# ---------------------------------------------------------------------------
# PATCH /api/vms/{id}/environment
# ---------------------------------------------------------------------------
def test_patch_environment_sets_user_set_source(client, db_session):
    from app.models.vm import VM

    vm = _create(client, name="a")
    r = client.patch(
        f"/api/vms/{vm['id']}/environment",
        json={"environment": "development", "rationale": "test reclass"},
    )
    assert r.status_code == 200, r.json()
    refreshed = db_session.get(VM, vm["id"])
    assert refreshed.environment == "development"
    assert refreshed.environment_source == "user_set"


def test_patch_environment_normalizes_aliases(client):
    vm = _create(client, name="b")
    r = client.patch(
        f"/api/vms/{vm['id']}/environment",
        json={"environment": "PROD"},
    )
    assert r.status_code == 200
    assert r.json()["environment"] == "production"


def test_patch_environment_rejects_unknown(client):
    vm = _create(client, name="c")
    r = client.patch(
        f"/api/vms/{vm['id']}/environment",
        json={"environment": "wibble"},
    )
    assert r.status_code == 400
    assert "wibble" in r.json()["detail"]


def test_patch_environment_audit_logged(client):
    vm = _create(client, name="d")
    client.patch(
        f"/api/vms/{vm['id']}/environment",
        json={"environment": "production", "rationale": "audit-test"},
    )
    rows = client.get("/api/audit?action=vm.environment.set&limit=10").json()
    assert any(r["details"].get("rationale") == "audit-test" for r in rows)


# ---------------------------------------------------------------------------
# POST /api/vms/bulk-set-environment
# ---------------------------------------------------------------------------
def test_bulk_set_environment_updates_all(client, db_session):
    from app.models.vm import VM

    ids = [_create(client, name=f"bulk-{i}")["id"] for i in range(1, 4)]
    r = client.post(
        "/api/vms/bulk-set-environment",
        json={"vm_ids": ids, "environment": "staging", "rationale": "bulk"},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["updated"] == 3
    for vid in ids:
        vm = db_session.get(VM, vid)
        assert vm.environment == "staging"
        assert vm.environment_source == "user_set"


def test_bulk_set_environment_404_for_missing_vm(client, db_session):
    from app.models.vm import VM

    ids = [_create(client, name=f"x-{i}")["id"] for i in range(1, 3)]
    r = client.post(
        "/api/vms/bulk-set-environment",
        json={"vm_ids": ids + [99999], "environment": "production"},
    )
    assert r.status_code == 404
    # Atomic — none of the real ids got updated.
    for vid in ids:
        assert db_session.get(VM, vid).environment in (None, "")


def test_bulk_set_environment_rejects_unknown_value(client):
    ids = [_create(client, name=f"y-{i}")["id"] for i in range(1, 3)]
    r = client.post(
        "/api/vms/bulk-set-environment",
        json={"vm_ids": ids, "environment": "garbage"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# POST /api/vms/redetect-environment
# ---------------------------------------------------------------------------
def test_redetect_skips_user_set_by_default(client, db_session):
    from app.models.vm import VM

    vm = _create(client, name="prod-web-01")  # auto-detected: production
    # Operator overrides to development + user_set.
    client.patch(
        f"/api/vms/{vm['id']}/environment",
        json={"environment": "development"},
    )

    r = client.post("/api/vms/redetect-environment", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["skipped_user_set"] == 1
    refreshed = db_session.get(VM, vm["id"])
    assert refreshed.environment == "development"  # preserved


def test_redetect_with_force_overwrites_user_set(client, db_session):
    from app.models.vm import VM

    vm = _create(client, name="prod-web-01")
    client.patch(
        f"/api/vms/{vm['id']}/environment",
        json={"environment": "development"},
    )

    r = client.post("/api/vms/redetect-environment", json={"force": True})
    assert r.status_code == 200
    body = r.json()
    assert body["skipped_user_set"] == 0
    refreshed = db_session.get(VM, vm["id"])
    # Detection cascade re-classifies based on name prefix → production.
    assert refreshed.environment == "production"
    assert refreshed.environment_source == "auto_detected"


def test_redetect_dry_run_does_not_persist(client, db_session):
    from app.models.vm import VM

    _create(client, name="prod-web-02")
    # First call wipes the auto-detected env (since dry_run is False
    # and the redetect re-runs detection). Manually clear it so the
    # dry_run shows projected changes.
    vm = db_session.scalars(
        __import__("sqlalchemy").select(VM).where(VM.name == "prod-web-02")
    ).one()
    vm.environment = None
    vm.environment_source = "unset"
    db_session.commit()

    r = client.post(
        "/api/vms/redetect-environment",
        json={"dry_run": True},
    )
    body = r.json()
    assert body["updated"] >= 1
    # Database wasn't touched.
    db_session.refresh(vm)
    assert vm.environment is None


def test_redetect_returns_breakdown(client):
    _create(client, name="prod-web-01")  # production
    _create(client, name="dev-server-01")  # development
    _create(client, name="random-thing")  # unknown

    r = client.post("/api/vms/redetect-environment", json={"force": True})
    body = r.json()
    assert body["scanned"] == 3
    # Summary includes each detected category at least once.
    assert "production" in body["summary_by_environment"]
    assert "development" in body["summary_by_environment"]
    assert body["still_unknown"] >= 1
    assert "random-thing" in body["still_unknown_samples"]


def test_redetect_can_scope_to_one_vcenter(client, db_session):
    # No vCenters seeded — the filter just narrows the query and
    # returns zero matches when vcenter_source_id has no VMs.
    r = client.post(
        "/api/vms/redetect-environment",
        json={"vcenter_source_id": 9999},
    )
    assert r.status_code == 200
    assert r.json()["scanned"] == 0
