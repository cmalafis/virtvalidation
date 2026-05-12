"""Tests for the validation trigger endpoints.

The SSH leg and Ollama leg are mocked — those are exercised at the unit
level by tests/test_ssh.py and tests/test_llm.py respectively. These tests
cover the full HTTP round-trip: the manual trigger, the polling endpoint,
the 200-not-404 contract on /validation/latest, and the bulk run-all flow.
"""

from __future__ import annotations

import pytest

from app.core import validation as validation_module
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


def _give_baseline(client, vm_id: int) -> None:
    """Persist a single baseline snapshot directly via the snapshots endpoint."""
    client.post(
        f"/api/vms/{vm_id}/snapshots",
        json={
            "ssh_user": "virtvalidate",
            "raw_data": {
                "meta": {"host": "10.0.0.5"},
                "services": [
                    {
                        "unit": "postgresql.service",
                        "load": "loaded",
                        "active": "active",
                        "sub": "running",
                        "description": "PostgreSQL 16",
                    }
                ],
                "ports": [{"proto": "tcp", "state": "LISTEN", "address": "0.0.0.0", "port": 5432}],
                "mounts": [],
                "network": {"interfaces": {}, "routes": [], "dns": []},
                "cron": {"user_crontabs": {}, "system": []},
            },
        },
    )


@pytest.fixture
def stub_validation(monkeypatch):
    """Replace ``run_validation`` so neither SSH nor Ollama are touched.

    The stub writes a real ``ValidationResult`` row using whatever verdict
    the test set on the ``calls`` attribute, so the API layer still goes
    through the real persist + audit path.
    """
    state = {"verdict": "pass"}
    calls: list[dict] = []

    def stub(db, vm, *, actor, progress=None, task_id=None, **_kwargs):
        from app.core.audit import record_audit
        from app.models.validation import ValidationResult, ValidationStatus

        verdict_map = {
            "pass": ValidationStatus.passed,
            "warn": ValidationStatus.warn,
            "fail": ValidationStatus.failed,
        }
        vm_status_map = {
            "pass": VMStatus.validated,
            "warn": VMStatus.validated,
            "fail": VMStatus.failed,
        }
        v = state["verdict"]
        row = ValidationResult(
            vm_id=vm.id,
            status=verdict_map[v],
            summary=f"stub verdict {v}",
            findings=[],
            remediation=[],
            diff={},
        )
        db.add(row)
        vm.status = vm_status_map[v]
        db.commit()
        db.refresh(row)
        record_audit(
            db,
            action="validation.completed",
            actor=actor,
            resource_type="validation",
            resource_id=row.id,
            details={
                "vm_id": vm.id,
                "vm_name": vm.name,
                "verdict": v,
                "finding_count": 0,
            },
        )
        db.commit()
        calls.append({"vm_id": vm.id, "actor": actor, "verdict": v})
        return row

    monkeypatch.setattr(validation_module, "run_validation", stub)
    yield {"state": state, "calls": calls}


@pytest.fixture(autouse=True)
def _reset_task_store():
    validation_module.task_store._tasks.clear()
    yield
    validation_module.task_store._tasks.clear()


# ---------------------------------------------------------------------------
# GET /validation/latest — 200 with null when none exist
# ---------------------------------------------------------------------------
def test_latest_validation_returns_200_with_null_when_none(client):
    vm = _enroll(client)
    r = client.get(f"/api/vms/{vm['id']}/validation/latest")
    assert r.status_code == 200
    assert r.json() == {"validation": None}


def test_latest_validation_returns_404_when_vm_missing(client):
    r = client.get("/api/vms/99999/validation/latest")
    assert r.status_code == 404


def test_latest_validation_returns_payload_after_run(client, stub_validation):
    vm = _enroll(client)
    _give_baseline(client, vm["id"])

    spawn = client.post(f"/api/vms/{vm['id']}/validate").json()
    assert spawn["task_id"]

    body = client.get(f"/api/vms/{vm['id']}/validation/latest").json()
    assert body["validation"] is not None
    assert body["validation"]["status"] == "pass"
    assert body["validation"]["vm_id"] == vm["id"]
    assert body["validation"]["summary"] == "stub verdict pass"


# ---------------------------------------------------------------------------
# POST /vms/{id}/validate
# ---------------------------------------------------------------------------
def test_trigger_validation_returns_task_handle(client, stub_validation):
    vm = _enroll(client)
    _give_baseline(client, vm["id"])

    r = client.post(f"/api/vms/{vm['id']}/validate")
    assert r.status_code == 202
    body = r.json()
    assert body["vm_id"] == vm["id"]
    assert body["task_id"]
    assert body["status"] in ("running", "completed")
    assert body["progress_percent"] in range(0, 101)
    assert body["current_step"] in {
        "queued",
        "ssh_collecting",
        "llm_reasoning",
        "storing",
        "completed",
    }


def test_trigger_validation_runs_in_background_and_persists_row(client, stub_validation):
    vm = _enroll(client)
    _give_baseline(client, vm["id"])

    spawn = client.post(f"/api/vms/{vm['id']}/validate").json()
    task_id = spawn["task_id"]

    status_resp = client.get(f"/api/vms/{vm['id']}/validate/{task_id}")
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "completed"
    assert body["progress_percent"] == 100
    assert body["current_step"] == "completed"
    assert body["validation_id"] is not None
    assert body["verdict"] == "pass"
    assert body["completed_at"]


def test_trigger_validation_400_when_no_baseline(client):
    vm = _enroll(client)
    r = client.post(f"/api/vms/{vm['id']}/validate")
    assert r.status_code == 400
    assert "baseline" in r.json()["detail"].lower()


def test_trigger_validation_404_for_unknown_vm(client):
    r = client.post("/api/vms/99999/validate")
    assert r.status_code == 404


def test_validation_status_404_for_unknown_task(client):
    vm = _enroll(client)
    r = client.get(f"/api/vms/{vm['id']}/validate/not-a-real-task")
    assert r.status_code == 404


def test_validation_status_404_when_task_belongs_to_different_vm(client, stub_validation):
    a = _enroll(client, name="a")
    b = _enroll(client, name="b")
    _give_baseline(client, a["id"])

    spawn = client.post(f"/api/vms/{a['id']}/validate").json()
    r = client.get(f"/api/vms/{b['id']}/validate/{spawn['task_id']}")
    assert r.status_code == 404


def test_trigger_validation_records_audit_with_user_actor(client, stub_validation):
    vm = _enroll(client)
    _give_baseline(client, vm["id"])
    client.post(f"/api/vms/{vm['id']}/validate")

    triggered = client.get("/api/audit?action=validation.triggered").json()
    assert len(triggered) == 1
    assert triggered[0]["actor"] == "user"
    assert triggered[0]["resource_id"] == str(vm["id"])
    assert triggered[0]["details"]["via"] == "single"

    completed = client.get("/api/audit?action=validation.completed").json()
    assert len(completed) == 1
    assert completed[0]["details"]["vm_name"] == vm["name"]
    assert completed[0]["details"]["verdict"] == "pass"


def test_trigger_validation_marks_task_failed_when_workflow_raises(client, monkeypatch):
    from app.core.validation import ValidationError

    def boom(db, vm, *, actor, progress=None, task_id=None, **_kwargs):
        raise ValidationError("ssh connection refused")

    monkeypatch.setattr(validation_module, "run_validation", boom)

    vm = _enroll(client)
    _give_baseline(client, vm["id"])
    spawn = client.post(f"/api/vms/{vm['id']}/validate").json()

    body = client.get(f"/api/vms/{vm['id']}/validate/{spawn['task_id']}").json()
    assert body["status"] == "failed"
    assert "ssh connection refused" in body["error"]


def test_trigger_validation_updates_vm_status_to_failed_on_critical(client, stub_validation):
    stub_validation["state"]["verdict"] = "fail"
    vm = _enroll(client)
    _give_baseline(client, vm["id"])

    client.post(f"/api/vms/{vm['id']}/validate")

    listing = client.get("/api/vms").json()["items"]
    target = next(v for v in listing if v["id"] == vm["id"])
    assert target["status"] == "failed"


# ---------------------------------------------------------------------------
# POST /api/validations/run-all
# ---------------------------------------------------------------------------
def test_run_all_spawns_one_task_per_vm_with_baseline(client, stub_validation):
    a = _enroll(client, name="a")
    b = _enroll(client, name="b")
    c = _enroll(client, name="c")
    _give_baseline(client, a["id"])
    _give_baseline(client, b["id"])
    # c has no baseline → should be skipped

    body = client.post("/api/validations/run-all").json()
    spawned_ids = sorted(s["vm_id"] for s in body["spawned"])
    assert spawned_ids == sorted([a["id"], b["id"]])
    assert body["skipped"] == [{"vm_id": c["id"], "reason": "no baseline captured"}]

    # Both validated VMs ended up with a result row.
    for vm in (a, b):
        latest = client.get(f"/api/vms/{vm['id']}/validation/latest").json()
        assert latest["validation"] is not None
        assert latest["validation"]["status"] == "pass"


def test_run_all_skips_vms_without_host(client, stub_validation):
    from sqlalchemy import update

    from app.core.db import SessionLocal
    from app.models.vm import VM

    a = _enroll(client, name="a")
    b = _enroll(client, name="b")
    _give_baseline(client, a["id"])
    _give_baseline(client, b["id"])

    session = SessionLocal()
    try:
        session.execute(
            update(VM).where(VM.id == b["id"]).values(ip_address=None, source_hostname="")
        )
        session.commit()
    finally:
        session.close()

    body = client.post("/api/validations/run-all").json()
    assert [s["vm_id"] for s in body["spawned"]] == [a["id"]]
    assert body["skipped"] == [{"vm_id": b["id"], "reason": "no host or IP address"}]


def test_run_all_audit_emits_per_vm_triggered_rows(client, stub_validation):
    a = _enroll(client, name="a")
    b = _enroll(client, name="b")
    _give_baseline(client, a["id"])
    _give_baseline(client, b["id"])

    client.post("/api/validations/run-all")

    rows = client.get("/api/audit?action=validation.triggered").json()
    assert len(rows) == 2
    for r in rows:
        assert r["details"]["via"] == "bulk"
