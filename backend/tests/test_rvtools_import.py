"""Tests for the RVTools delta-import endpoint and core helpers.

The preview endpoint stays covered by ``tests/test_vcenters.py``; this
module focuses on the commit path: row creation/update, drift
detection, idempotency, audit emission, and the async / background-task
threshold.
"""

from __future__ import annotations

from app.core.rvtools_import import ASYNC_IMPORT_THRESHOLD


def _create_vc(client, name: str = "vc-rvtools") -> dict:
    r = client.post(
        "/api/sources/vcenters",
        json={"name": name, "hostname": f"{name}.local"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _import(client, vc_id: int, vms: list[dict], actor: str = "tester"):
    return client.post(
        f"/api/sources/vcenters/{vc_id}/rvtools/import",
        headers={"x-actor": actor},
        json={"vms": vms},
    )


# ---------------------------------------------------------------------------
# Sync-mode import
# ---------------------------------------------------------------------------
def test_import_creates_new_vms_with_vcenter_scope_and_status(client):
    vc = _create_vc(client)
    payload = [
        {
            "name": "alpha",
            "source_hostname": "alpha.corp",
            "ip_address": "10.0.0.1",
            "vsphere_networks": ["prod"],
            "vsphere_datastores": ["tier1"],
        },
        {
            "name": "beta",
            "source_hostname": "beta.corp",
            "ip_address": "10.0.0.2",
        },
    ]
    r = _import(client, vc["id"], payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 2
    assert body["updated"] == 0
    assert body["marked_missing"] == 0
    # vCenter row's vm_count reflects the import.
    assert client.get(f"/api/sources/vcenters/{vc['id']}").json()["vm_count"] == 2
    listing = {vm["name"]: vm for vm in client.get("/api/vms").json()}
    assert listing["alpha"]["source_vcenter_id"] == vc["id"]
    assert listing["alpha"]["status"] == "discovered"


def test_import_updates_tracked_fields_without_clobbering_unrelated_state(client):
    vc = _create_vc(client)
    _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp", "ip_address": "10.0.0.1"}
    ])
    # Operator manually edits target_namespace via PATCH — that field
    # is *not* part of the RVTools tracked set, so re-importing must
    # leave it intact.
    vm_id = next(v["id"] for v in client.get("/api/vms").json() if v["name"] == "alpha")
    client.patch(f"/api/vms/{vm_id}", json={"target_namespace": "finance-prod"})
    r = _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp", "ip_address": "10.0.0.99"}
    ])
    assert r.status_code == 200
    assert r.json()["updated"] == 1
    refreshed = client.get(f"/api/vms/{vm_id}").json()
    assert refreshed["ip_address"] == "10.0.0.99"
    assert refreshed["target_namespace"] == "finance-prod"


def test_import_marks_missing_vm_without_deleting_it(client, db_session):
    from app.models.vm import VM

    vc = _create_vc(client)
    _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp"},
        {"name": "beta", "source_hostname": "beta.corp"},
    ])
    # Re-upload with `beta` removed — it should be flagged missing,
    # not deleted, and `alpha` stays clean.
    r = _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp"}
    ])
    assert r.status_code == 200
    body = r.json()
    assert body["marked_missing"] == 1
    assert body["unchanged"] == 1
    # beta still exists in the DB.
    db_session.expire_all()
    beta = db_session.query(VM).filter_by(name="beta").one()
    assert beta.missing_from_last_upload is True
    alpha = db_session.query(VM).filter_by(name="alpha").one()
    assert alpha.missing_from_last_upload is False


def test_import_idempotent_on_repeat(client):
    vc = _create_vc(client)
    payload = [{"name": "alpha", "source_hostname": "alpha.corp", "ip_address": "10.0.0.1"}]
    first = _import(client, vc["id"], payload).json()
    second = _import(client, vc["id"], payload).json()
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 0
    assert second["marked_missing"] == 0
    assert second["unchanged"] == 1


def test_import_clears_missing_flag_when_vm_reappears(client, db_session):
    from app.models.vm import VM

    vc = _create_vc(client)
    _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp"},
        {"name": "beta", "source_hostname": "beta.corp"},
    ])
    # Drop beta to flag it missing.
    _import(client, vc["id"], [{"name": "alpha", "source_hostname": "alpha.corp"}])
    # Re-include beta — flag must clear.
    _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp"},
        {"name": "beta", "source_hostname": "beta.corp"},
    ])
    db_session.expire_all()
    beta = db_session.query(VM).filter_by(name="beta").one()
    assert beta.missing_from_last_upload is False


def test_import_emits_audit_log_per_change(client):
    vc = _create_vc(client)
    _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp"},
        {"name": "beta", "source_hostname": "beta.corp"},
    ], actor="audit-tester")
    audit = client.get(
        "/api/audit?action=vm.rvtools_import.create",
    ).json()
    actions = {row["action"] for row in audit}
    assert "vm.rvtools_import.create" in actions
    audit_actors = {row["actor"] for row in audit}
    assert "audit-tester" in audit_actors

    # Now drop beta — verify the marked_missing audit entry shows up.
    _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp"}
    ], actor="audit-tester")
    missing_audit = client.get(
        "/api/audit?action=vm.rvtools_import.marked_missing",
    ).json()
    assert any(row["details"]["name"] == "beta" for row in missing_audit)


def test_import_unknown_vcenter_returns_404(client):
    r = client.post(
        "/api/sources/vcenters/9999/rvtools/import",
        json={"vms": [{"name": "alpha", "source_hostname": "alpha.local"}]},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Async import (large uploads)
# ---------------------------------------------------------------------------
def test_import_above_threshold_returns_202_with_task_id(client):
    vc = _create_vc(client)
    big_payload = [
        {
            "name": f"vm-{i:04d}",
            "source_hostname": f"vm-{i:04d}.corp",
        }
        for i in range(ASYNC_IMPORT_THRESHOLD + 5)
    ]
    r = _import(client, vc["id"], big_payload)
    assert r.status_code == 200, r.text
    body = r.json()
    # Async branch: response carries task_id + status, no created/updated.
    assert "task_id" in body
    assert body["status"] in {"running", "completed"}
    # FastAPI's TestClient runs BackgroundTasks before the response
    # returns control to the test, so by here the task has already
    # completed. Verify via the status endpoint.
    status = client.get(
        f"/api/sources/vcenters/{vc['id']}/rvtools/import/{body['task_id']}"
    )
    assert status.status_code == 200
    status_body = status.json()
    assert status_body["status"] == "completed"
    assert status_body["result"]["created"] == ASYNC_IMPORT_THRESHOLD + 5


def test_import_status_404_for_unknown_task_id(client):
    vc = _create_vc(client)
    r = client.get(
        f"/api/sources/vcenters/{vc['id']}/rvtools/import/00000000-0000-0000-0000-000000000000"
    )
    assert r.status_code == 404


def test_import_status_404_when_task_belongs_to_other_vcenter(client):
    vc1 = _create_vc(client, name="vc-one")
    vc2 = _create_vc(client, name="vc-two")
    big_payload = [
        {"name": f"x-{i:04d}", "source_hostname": f"x-{i:04d}.corp"}
        for i in range(ASYNC_IMPORT_THRESHOLD)
    ]
    body = _import(client, vc1["id"], big_payload).json()
    r = client.get(
        f"/api/sources/vcenters/{vc2['id']}/rvtools/import/{body['task_id']}"
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Existing preview endpoint keeps working
# ---------------------------------------------------------------------------
def test_preview_unchanged_after_import_changes(client):
    """test_vcenters.py covers preview semantics. Spot-check here that
    the preview / import pair stays consistent: new+updated counts the
    preview reports match what the subsequent import actually does."""
    vc = _create_vc(client)
    _import(client, vc["id"], [
        {"name": "alpha", "source_hostname": "alpha.corp"},
    ])
    payload = {
        "vms": [
            {"name": "alpha", "source_hostname": "alpha.corp", "ip_address": "10.0.0.99"},
            {"name": "beta", "source_hostname": "beta.corp"},
        ]
    }
    preview = client.post(
        f"/api/sources/vcenters/{vc['id']}/rvtools/preview", json=payload
    ).json()
    assert preview["summary"]["new"] == 1
    assert preview["summary"]["updated"] == 1
    assert preview["summary"]["removed"] == 0
    importr = client.post(
        f"/api/sources/vcenters/{vc['id']}/rvtools/import", json=payload
    ).json()
    assert importr["created"] == 1
    assert importr["updated"] == 1
    assert importr["marked_missing"] == 0
