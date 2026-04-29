"""Tests for the audit log model, middleware, and endpoint."""

from __future__ import annotations

from sqlalchemy import select

from app.core.audit import infer_action, record_audit
from app.models.audit import AuditLog

# ---------- infer_action mapping ----------


def test_infer_action_maps_known_routes():
    assert infer_action("POST", "/api/vms") == ("vm.create", "vm", None)
    assert infer_action("POST", "/api/vms/bulk") == ("vm.bulk_create", "vm", None)
    assert infer_action("PATCH", "/api/vms/42") == ("vm.update", "vm", "42")
    assert infer_action("DELETE", "/api/vms/7") == ("vm.delete", "vm", "7")
    assert infer_action("POST", "/api/vms/3/snapshots") == ("baseline.create", "baseline", "3")
    assert infer_action("POST", "/api/plans") == ("plan.create", "plan", None)
    assert infer_action("PUT", "/api/settings") == ("settings.update", "settings", None)


def test_infer_action_falls_back_for_unmatched():
    action, rtype, rid = infer_action("POST", "/something/random")
    assert action == "api.post"
    assert rtype is None
    assert rid is None


# ---------- record_audit helper ----------


def test_record_audit_persists_row(db_session):
    record_audit(
        db_session,
        action="custom.thing",
        actor="bob",
        resource_type="thing",
        resource_id=42,
        details={"k": "v"},
    )
    db_session.commit()
    rows = list(db_session.scalars(select(AuditLog)).all())
    assert len(rows) == 1
    assert rows[0].action == "custom.thing"
    assert rows[0].actor == "bob"
    assert rows[0].resource_id == "42"  # coerced to string
    assert rows[0].details == {"k": "v"}


# ---------- middleware integration ----------


def test_post_vm_creates_audit_entry(client, mock_vm_payload):
    r = client.post("/api/vms", json=mock_vm_payload)
    assert r.status_code == 201

    audit = client.get("/api/audit").json()
    assert len(audit) == 1
    entry = audit[0]
    assert entry["action"] == "vm.create"
    assert entry["resource_type"] == "vm"
    assert entry["actor"] == "anonymous"
    assert entry["details"]["method"] == "POST"
    assert entry["details"]["path"] == "/api/vms"
    assert entry["details"]["status_code"] == 201


def test_get_requests_do_not_create_audit_entries(client):
    client.get("/api/vms")
    client.get("/api/health/postgres")
    audit = client.get("/api/audit").json()
    assert audit == []


def test_failed_post_is_still_audited(client):
    r = client.post("/api/vms", json={})  # missing required fields → 422
    assert r.status_code == 422
    audit = client.get("/api/audit").json()
    assert len(audit) == 1
    assert audit[0]["details"]["status_code"] == 422
    assert audit[0]["action"] == "vm.create"


def test_audit_endpoint_does_not_self_log(client, mock_vm_payload):
    client.post("/api/vms", json=mock_vm_payload)
    client.get("/api/audit")
    client.get("/api/audit")
    # Only the original POST should be in there
    audit = client.get("/api/audit").json()
    assert len(audit) == 1
    assert audit[0]["action"] == "vm.create"


def test_audit_filters_by_action(client, mock_vm_payload):
    client.post("/api/vms", json=mock_vm_payload)
    client.put("/api/settings", json={"schedule_preset": "hourly"})

    r = client.get("/api/audit?action=vm.create")
    body = r.json()
    assert len(body) == 1
    assert body[0]["action"] == "vm.create"


def test_audit_filters_by_resource_type(client, mock_vm_payload):
    client.post("/api/vms", json=mock_vm_payload)
    client.put("/api/settings", json={"schedule_preset": "hourly"})

    r = client.get("/api/audit?resource_type=settings")
    body = r.json()
    assert len(body) == 1
    assert body[0]["resource_type"] == "settings"


def test_audit_endpoint_respects_limit(client):
    for i in range(5):
        client.post("/api/vms", json={"name": f"vm-{i}", "source_hostname": f"vm-{i}.local"})

    r = client.get("/api/audit?limit=2")
    assert len(r.json()) == 2


def test_audit_orders_newest_first(client):
    client.post("/api/vms", json={"name": "vm-a", "source_hostname": "a.local"})
    client.post("/api/vms", json={"name": "vm-b", "source_hostname": "b.local"})

    audit = client.get("/api/audit").json()
    # The bulk-of-2 ordering means index 0 is the most recent (vm-b)
    assert audit[0]["details"]["status_code"] == 201
    assert audit[1]["details"]["status_code"] == 201
    assert audit[0]["timestamp"] >= audit[1]["timestamp"]


def test_audit_picks_up_x_actor_header(client, mock_vm_payload):
    client.post("/api/vms", json=mock_vm_payload, headers={"x-actor": "alice@corp"})
    audit = client.get("/api/audit").json()
    assert audit[0]["actor"] == "alice@corp"


def test_audit_logs_patch_with_resource_id(client, mock_vm_payload):
    created = client.post("/api/vms", json=mock_vm_payload).json()
    client.patch(f"/api/vms/{created['id']}", json={"role": "updated"})

    r = client.get("/api/audit?action=vm.update").json()
    assert len(r) == 1
    assert r[0]["resource_id"] == str(created["id"])


def test_audit_logs_bulk_create(client):
    r = client.post(
        "/api/vms/bulk",
        json={"vms": [{"name": "x", "source_hostname": "x.local"}]},
    )
    assert r.status_code == 200

    audit = client.get("/api/audit?action=vm.bulk_create").json()
    assert len(audit) == 1
    assert audit[0]["resource_type"] == "vm"


def test_audit_invalid_limit_rejected(client):
    r = client.get("/api/audit?limit=0")
    assert r.status_code == 422
    r = client.get("/api/audit?limit=10000")
    assert r.status_code == 422
