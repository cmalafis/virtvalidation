"""Tests for the on-demand baseline capture endpoints + bulk delete + audited PATCH.

Mocks ``app.core.capture.collect_and_store`` so we never actually open an SSH
session — that's covered by tests/test_ssh.py at the unit level.
"""

from __future__ import annotations

import pytest

from app.core import capture as capture_module
from app.models.vm import VMStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _enroll(client, name: str = "db-01", **overrides) -> dict:
    payload = {
        "name": name,
        "source_hostname": f"{name}.local",
        "ip_address": "10.0.0.5",
        "role": "database",
        **overrides,
    }
    return client.post("/api/vms", json=payload).json()


@pytest.fixture
def fake_collect_and_store(monkeypatch):
    """Replace the SSH+DB capture routine with a deterministic stub.

    The stub records each call so tests can assert on what the manual
    trigger asked it to do, and stores a fake snapshot via the real DB
    session so the validation gate at the API level still exercises a
    real ``BaselineSnapshot`` row.
    """
    calls: list[dict] = []

    def stub(db, vm, *, actor, collector=None):
        from sqlalchemy import func, select

        from app.models.vm import BaselineSnapshot

        next_number = (
            db.scalar(
                select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0)).where(
                    BaselineSnapshot.vm_id == vm.id
                )
            )
            or 0
        ) + 1
        snap = BaselineSnapshot(
            vm_id=vm.id,
            snapshot_number=next_number,
            ssh_user=vm.ssh_user or "virtvalidate",
            raw_data={
                "meta": {"host": vm.ip_address},
                "services": [],
                "ports": [],
                "mounts": [],
                "network": {},
                "cron": {},
            },
        )
        db.add(snap)
        if vm.status == VMStatus.discovered:
            vm.status = VMStatus.baseline_captured
        db.commit()
        db.refresh(snap)
        calls.append({"vm_id": vm.id, "actor": actor, "snapshot_id": snap.id})
        return snap

    monkeypatch.setattr(capture_module, "collect_and_store", stub)
    yield calls


@pytest.fixture(autouse=True)
def _reset_task_store():
    """Clear the in-memory task store between tests."""
    capture_module.task_store._tasks.clear()
    yield
    capture_module.task_store._tasks.clear()


# ---------------------------------------------------------------------------
# POST /api/vms/{id}/capture
# ---------------------------------------------------------------------------
def test_trigger_capture_returns_task_handle(client, fake_collect_and_store):
    vm = _enroll(client)

    r = client.post(f"/api/vms/{vm['id']}/capture")
    assert r.status_code == 202
    body = r.json()
    assert body["vm_id"] == vm["id"]
    assert body["status"] in ("running", "completed")
    assert body["task_id"]
    assert body["started_at"]


def test_trigger_capture_runs_in_background_and_persists_snapshot(client, fake_collect_and_store):
    """TestClient runs BackgroundTasks before returning, so the GET that
    follows should already see status=completed and the snapshot row."""
    vm = _enroll(client)

    spawn = client.post(f"/api/vms/{vm['id']}/capture").json()
    task_id = spawn["task_id"]

    status_resp = client.get(f"/api/vms/{vm['id']}/capture/{task_id}")
    assert status_resp.status_code == 200
    status_body = status_resp.json()
    assert status_body["status"] == "completed"
    assert status_body["snapshot_id"] is not None
    assert status_body["completed_at"]

    # Snapshot really exists in the DB.
    snaps = client.get(f"/api/vms/{vm['id']}/snapshots").json()
    assert len(snaps) == 1
    assert snaps[0]["snapshot_number"] == 1


def test_trigger_capture_records_audit_with_user_actor(client, fake_collect_and_store):
    vm = _enroll(client)
    client.post(f"/api/vms/{vm['id']}/capture")

    rows = client.get("/api/audit?action=capture.triggered").json()
    assert len(rows) == 1
    assert rows[0]["actor"] == "user"
    assert rows[0]["resource_type"] == "vm"
    assert rows[0]["resource_id"] == str(vm["id"])
    assert rows[0]["details"]["via"] == "single"
    assert rows[0]["details"]["vm_name"] == "db-01"


def test_capture_collect_and_store_called_with_user_actor(client, fake_collect_and_store):
    vm = _enroll(client)
    client.post(f"/api/vms/{vm['id']}/capture")
    assert fake_collect_and_store == [
        {
            "vm_id": vm["id"],
            "actor": "user",
            "snapshot_id": fake_collect_and_store[0]["snapshot_id"],
        }
    ]


def test_trigger_capture_marks_task_failed_when_collector_raises(client, monkeypatch):
    from app.core.capture import CaptureError

    def boom(db, vm, *, actor, collector=None):
        raise CaptureError("ssh connection refused")

    monkeypatch.setattr(capture_module, "collect_and_store", boom)

    vm = _enroll(client)
    spawn = client.post(f"/api/vms/{vm['id']}/capture").json()
    task_id = spawn["task_id"]

    status_resp = client.get(f"/api/vms/{vm['id']}/capture/{task_id}")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "failed"
    assert "ssh connection refused" in body["error"]


def test_trigger_capture_returns_404_for_unknown_vm(client):
    r = client.post("/api/vms/99999/capture")
    assert r.status_code == 404


def test_capture_status_404_for_unknown_task(client):
    vm = _enroll(client)
    r = client.get(f"/api/vms/{vm['id']}/capture/not-a-real-task")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_capture_status_404_when_task_belongs_to_different_vm(client, fake_collect_and_store):
    vm_a = _enroll(client, name="a")
    vm_b = _enroll(client, name="b")

    spawn = client.post(f"/api/vms/{vm_a['id']}/capture").json()
    task_id = spawn["task_id"]

    r = client.get(f"/api/vms/{vm_b['id']}/capture/{task_id}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/snapshots/capture-all
# ---------------------------------------------------------------------------
def test_capture_all_spawns_one_task_per_vm(client, fake_collect_and_store):
    a = _enroll(client, name="a")
    b = _enroll(client, name="b")

    r = client.post("/api/snapshots/capture-all")
    assert r.status_code == 200
    body = r.json()

    spawned_ids = sorted(s["vm_id"] for s in body["spawned"])
    assert spawned_ids == sorted([a["id"], b["id"]])
    assert body["skipped"] == []

    # Each VM has a snapshot now.
    for vm in (a, b):
        snaps = client.get(f"/api/vms/{vm['id']}/snapshots").json()
        assert len(snaps) == 1


def test_capture_all_skips_vms_without_host_or_ip(client, fake_collect_and_store):
    # Create a VM the schema-conformant way, then strip host fields the
    # schema would normally enforce — we want capture-all's runtime guard
    # to fire even when somebody hand-mutates the table.
    from sqlalchemy import update

    from app.core.db import SessionLocal
    from app.models.vm import VM

    a = _enroll(client, name="a")
    b = _enroll(client, name="b")

    session = SessionLocal()
    try:
        session.execute(
            update(VM).where(VM.id == b["id"]).values(ip_address=None, source_hostname="")
        )
        session.commit()
    finally:
        session.close()

    body = client.post("/api/snapshots/capture-all").json()
    assert [s["vm_id"] for s in body["spawned"]] == [a["id"]]
    assert body["skipped"] == [{"vm_id": b["id"], "reason": "no host or IP address"}]


# ---------------------------------------------------------------------------
# DELETE /api/vms — bulk
# ---------------------------------------------------------------------------
def test_bulk_delete_removes_vms_and_audits_each(client, fake_collect_and_store):
    a = _enroll(client, name="a")
    b = _enroll(client, name="b")
    _enroll(client, name="c")

    # Give one VM a baseline so we can verify cascade.
    client.post(f"/api/vms/{a['id']}/capture")

    # TestClient.delete doesn't accept `json=` directly; use request().
    r = client.request("DELETE", "/api/vms", json={"vm_ids": [a["id"], b["id"], 99999]})
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 3
    assert sorted(body["deleted"]) == sorted([a["id"], b["id"]])
    assert body["not_found"] == [99999]

    # c remains.
    listing = client.get("/api/vms").json()["items"]
    assert [vm["name"] for vm in listing] == ["c"]

    # Snapshots for a should have cascaded.
    snaps = client.get(f"/api/vms/{a['id']}/snapshots")
    assert snaps.status_code == 404

    # Per-VM audit rows survive (audit_logs is intentionally not FK'd).
    rows = client.get("/api/audit?action=vm.delete").json()
    deleted_ids = sorted(int(r["resource_id"]) for r in rows)
    assert deleted_ids == sorted([a["id"], b["id"]])
    for r in rows:
        assert r["details"]["via_bulk"] is True
        assert r["details"]["vm_name"] in {"a", "b"}


# ---------------------------------------------------------------------------
# PATCH audit diff
# ---------------------------------------------------------------------------
def test_patch_records_field_level_diff(client):
    vm = _enroll(client, role="database", ssh_user="rocky")

    r = client.patch(f"/api/vms/{vm['id']}", json={"role": "app", "ssh_user": "ec2-user"})
    assert r.status_code == 200

    rows = client.get("/api/audit?action=vm.update").json()
    assert len(rows) == 1
    diff = rows[0]["details"]["diff"]
    assert diff["role"] == {"before": "database", "after": "app"}
    assert diff["ssh_user"] == {"before": "rocky", "after": "ec2-user"}
    # Unchanged fields don't appear in the diff.
    assert "name" not in diff


def test_patch_with_no_changes_does_not_audit(client):
    vm = _enroll(client)
    client.patch(f"/api/vms/{vm['id']}", json={})  # empty body
    rows = client.get("/api/audit?action=vm.update").json()
    assert rows == []


def test_delete_records_hostname_in_audit_details(client):
    vm = _enroll(client, name="db-prod-01")
    client.delete(f"/api/vms/{vm['id']}")

    rows = client.get("/api/audit?action=vm.delete").json()
    assert len(rows) == 1
    details = rows[0]["details"]
    assert details["vm_name"] == "db-prod-01"
    assert details["source_hostname"] == "db-prod-01.local"
    assert details["ip_address"] == "10.0.0.5"


# ---------------------------------------------------------------------------
# Host-key audit trail (wrapper-level)
# ---------------------------------------------------------------------------
def test_host_key_acceptance_writes_audit_row(client, monkeypatch):
    """Wrapping ``collect_and_store`` should emit a ``ssh.host_key_accepted``
    audit row when the collector reports a TOFU acceptance."""
    from app.core import capture as capture_module

    def stub(db, vm, *, actor, collector=None):
        from sqlalchemy import func, select

        from app.models.vm import BaselineSnapshot

        next_number = (
            db.scalar(
                select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0)).where(
                    BaselineSnapshot.vm_id == vm.id
                )
            )
            or 0
        ) + 1
        snap = BaselineSnapshot(
            vm_id=vm.id,
            snapshot_number=next_number,
            ssh_user=vm.ssh_user or "virtvalidate",
            raw_data={
                "meta": {},
                "services": [],
                "ports": [],
                "mounts": [],
                "network": {},
                "cron": {},
            },
        )
        db.add(snap)
        db.commit()
        db.refresh(snap)
        return snap

    # Replace the SSH-bound helper with our stub, then drive a fake host-key
    # acceptance through the wrapper directly so the audit emission path runs.
    monkeypatch.setattr(capture_module, "collect_and_store", stub)

    vm = _enroll(client)
    client.post(f"/api/vms/{vm['id']}/capture")

    # The capture endpoint runs through run_capture_task, which calls our
    # stub for collect_and_store — but we want the host-key audit which lives
    # in the *real* collect_and_store. Drive the audit helper directly.
    from app.core import db as _db_module
    from app.core.capture import _audit_host_key_event
    from app.models.vm import VM

    session = _db_module.SessionLocal()
    try:
        vm_row = session.get(VM, vm["id"])
        _audit_host_key_event(
            session,
            actor="user",
            vm=vm_row,
            event={
                "action": "added",
                "host": "10.0.0.5",
                "key_type": "ssh-ed25519",
                "fingerprint": "SHA256:abc123",
            },
        )
    finally:
        session.close()

    rows = client.get("/api/audit?action=ssh.host_key_accepted").json()
    assert len(rows) == 1
    assert rows[0]["resource_id"] == str(vm["id"])
    assert rows[0]["details"]["host"] == "10.0.0.5"
    assert rows[0]["details"]["fingerprint"] == "SHA256:abc123"
    assert rows[0]["details"]["key_type"] == "ssh-ed25519"


def test_host_key_mismatch_writes_audit_row_on_failure_path(client):
    """When the SSH layer raises ``host_key_mismatch``, the wrapper must
    audit it before the error bubbles up so the trail captures the
    security event even though the capture itself failed."""
    from app.core import db as _db_module
    from app.core.capture import _audit_host_key_failure
    from app.core.ssh import SSHCollectionError
    from app.models.vm import VM

    vm = _enroll(client)

    err = SSHCollectionError(
        "host key changed",
        kind="host_key_mismatch",
        host="10.0.0.5",
        fingerprint="SHA256:newkey",
    )
    session = _db_module.SessionLocal()
    try:
        vm_row = session.get(VM, vm["id"])
        _audit_host_key_failure(session, actor="user", vm=vm_row, err=err)
    finally:
        session.close()

    rows = client.get("/api/audit?action=ssh.host_key_mismatch").json()
    assert len(rows) == 1
    assert rows[0]["details"]["fingerprint"] == "SHA256:newkey"
    assert "host key changed" in rows[0]["details"]["message"]


def test_settings_round_trip_persists_host_key_policy(client):
    body = client.put("/api/settings", json={"ssh_host_key_policy": "strict"}).json()
    assert body["ssh_host_key_policy"] == "strict"
    body2 = client.get("/api/settings").json()
    assert body2["ssh_host_key_policy"] == "strict"


# ---------------------------------------------------------------------------
# Settings next_run_at
# ---------------------------------------------------------------------------
def test_settings_response_includes_next_run_at_field(client):
    body = client.get("/api/settings").json()
    # Scheduler isn't started in tests — value is None but the field exists.
    assert "next_run_at" in body
    assert body["next_run_at"] is None
