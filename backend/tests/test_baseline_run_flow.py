"""Integration tests for the wave-scoped baseline + validation flow.

The collection engine is mocked at the orchestrator level — we substitute
``run_collection_batch`` with a fake that produces controlled results
without real SSH. That keeps the test deterministic and fast while still
exercising the full route → background-task → DB-persistence path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core import config as cfg_mod
from app.core.collection.engine import CollectionResult, VMTarget
from app.models.baseline_run import BaselineRunStatus, VMCollectionStatus
from app.models.plan import MigrationPlan
from app.models.validation_run import ValidationRunStatus, VMValidationVerdict
from app.models.vm import VM
from app.services import ssh_key_service


def _full_state(host: str) -> dict:
    return {
        "meta": {"host": host, "hostname": f"{host}.corp", "kernel": "5.14.0", "os": {}},
        "services": [{"unit": "sshd.service", "active": "active", "sub": "running"}],
        "network": {"interfaces": {"eth0": {}}, "routes": ["default via 10.0.0.1"], "dns": []},
        "ports": [{"proto": "tcp", "address": "0.0.0.0", "port": 22}],
        "mounts": [{"target": "/", "source": "/dev/sda1", "fstype": "xfs"}],
        "cron": {"user_crontabs": {}, "system": []},
    }


@pytest.fixture
def isolated_key_dir(tmp_path, monkeypatch):
    target = tmp_path / "keys" / "id_ed25519"
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_path", str(target))
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_algorithm", "ed25519")
    monkeypatch.setattr(cfg_mod.settings, "fips_mode", False)
    return tmp_path / "keys"


@pytest.fixture
def plan_with_wave(db_session):
    """Create a MigrationPlan + 3 VMs + a wave referencing all three."""
    vms = []
    for i in range(3):
        vm = VM(
            name=f"vm-{i}",
            source_hostname=f"vm-{i}.vmware.local",
            ip_address=f"10.0.0.{i + 10}",
            ssh_user="virtvalidate",
        )
        db_session.add(vm)
        vms.append(vm)
    db_session.flush()

    plan = MigrationPlan(
        name="test-plan",
        vm_ids=[vm.id for vm in vms],
        waves=[
            {
                "wave_number": 1,
                "name": "Wave 1",
                "vm_ids": [vm.id for vm in vms],
                "vm_names": [vm.name for vm in vms],
                "rationale": "",
            }
        ],
        model="",
        status="complete",
    )
    db_session.add(plan)
    db_session.commit()
    return plan, vms


# ---------------------------------------------------------------------------
# Baseline kick-off + read
# ---------------------------------------------------------------------------
def test_baseline_kickoff_returns_202_and_creates_per_vm_rows(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    plan, vms = plan_with_wave

    # Create a key via the multi-key service.
    key = ssh_key_service.generate_keypair(db_session, name="wave-key")
    key_id = key.id

    # Patch the orchestrator so the background task is fast + deterministic.
    async def fake_batch(*, targets, engine, max_concurrency, on_vm_complete):
        results = []
        for t in targets:
            result = CollectionResult(
                vm_id=t.vm_id,
                succeeded=True,
                collected_data=_full_state(t.host),
                probes_run=["vm_identity"],
                completed_at=datetime.now(timezone.utc),
            )
            if on_vm_complete:
                on_vm_complete(result)
            results.append(result)
        return results

    monkeypatch.setattr(
        "app.core.collection.wave_jobs.run_collection_batch", fake_batch
    )

    r = client.post(
        f"/api/plans/{plan.id}/waves/1/baseline",
        json={"ssh_key_id": key_id},
    )
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["baseline_run_id"] >= 1
    assert body["status_url"].startswith("/api/baseline-runs/")

    # GET reflects the completed run.
    run_id = body["baseline_run_id"]
    detail = client.get(f"/api/baseline-runs/{run_id}").json()
    assert detail["status"] == "completed"
    assert detail["total_vms"] == 3
    assert detail["captured_vms"] == 3
    assert detail["failed_vms"] == 0
    assert len(detail["baselines"]) == 3
    for b in detail["baselines"]:
        assert b["status"] == "captured"
        assert b["collected_data"] is not None


def test_baseline_partial_success_records_failures(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    """7 succeed / 3 fail — run is still 'completed' (partial-success is valid)."""
    plan, vms = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="partial-key")
    key_id = key.id

    async def fake_batch(*, targets, engine, max_concurrency, on_vm_complete):
        # First VM fails, second is unreachable, third succeeds.
        for i, t in enumerate(targets):
            if i == 0:
                result = CollectionResult(
                    vm_id=t.vm_id,
                    succeeded=False,
                    failure_category="auth_failed",
                    failure_detail="bad key",
                    completed_at=datetime.now(timezone.utc),
                )
            elif i == 1:
                result = CollectionResult(
                    vm_id=t.vm_id,
                    succeeded=False,
                    failure_category="unreachable",
                    failure_detail="Connection refused",
                    completed_at=datetime.now(timezone.utc),
                )
            else:
                result = CollectionResult(
                    vm_id=t.vm_id,
                    succeeded=True,
                    collected_data=_full_state(t.host),
                    completed_at=datetime.now(timezone.utc),
                )
            if on_vm_complete:
                on_vm_complete(result)
        return []

    monkeypatch.setattr(
        "app.core.collection.wave_jobs.run_collection_batch", fake_batch
    )

    r = client.post(
        f"/api/plans/{plan.id}/waves/1/baseline",
        json={"ssh_key_id": key_id},
    )
    assert r.status_code == 202
    run_id = r.json()["baseline_run_id"]
    detail = client.get(f"/api/baseline-runs/{run_id}").json()
    assert detail["status"] == "completed", (
        "Partial-success is still completed, not failed"
    )
    assert detail["captured_vms"] == 1
    assert detail["failed_vms"] == 2
    failed = [b for b in detail["baselines"] if b["status"] == "failed"]
    assert len(failed) == 2
    assert {b["failure_category"] for b in failed} == {"auth_failed", "unreachable"}


def test_baseline_rejects_retired_key(
    isolated_key_dir, client, plan_with_wave, db_session
):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="retired-key")
    ssh_key_service.retire_key(db_session, key.id)

    r = client.post(
        f"/api/plans/{plan.id}/waves/1/baseline",
        json={"ssh_key_id": key.id},
    )
    assert r.status_code == 422
    assert "retired" in r.json()["detail"]


def test_baseline_rejects_unknown_wave(isolated_key_dir, client, plan_with_wave, db_session):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="k")
    r = client.post(
        f"/api/plans/{plan.id}/waves/99/baseline",
        json={"ssh_key_id": key.id},
    )
    assert r.status_code == 422
    assert "no wave numbered 99" in r.json()["detail"]


def test_retry_failed_creates_new_run_with_only_failed_vms(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    plan, vms = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="retry-key")
    key_id = key.id

    async def first_run(*, targets, engine, max_concurrency, on_vm_complete):
        # First and last succeed, middle fails.
        for i, t in enumerate(targets):
            if i == 1:
                result = CollectionResult(
                    vm_id=t.vm_id,
                    succeeded=False,
                    failure_category="timeout",
                    failure_detail="timed out",
                    completed_at=datetime.now(timezone.utc),
                )
            else:
                result = CollectionResult(
                    vm_id=t.vm_id,
                    succeeded=True,
                    collected_data=_full_state(t.host),
                    completed_at=datetime.now(timezone.utc),
                )
            if on_vm_complete:
                on_vm_complete(result)
        return []

    monkeypatch.setattr(
        "app.core.collection.wave_jobs.run_collection_batch", first_run
    )

    r = client.post(
        f"/api/plans/{plan.id}/waves/1/baseline",
        json={"ssh_key_id": key_id},
    )
    original_run_id = r.json()["baseline_run_id"]
    first_detail = client.get(f"/api/baseline-runs/{original_run_id}").json()
    assert first_detail["failed_vms"] == 1

    # Now retry — the new run should only have 1 VM.
    seen_vm_ids: list[int] = []

    async def second_run(*, targets, engine, max_concurrency, on_vm_complete):
        for t in targets:
            seen_vm_ids.append(t.vm_id)
            result = CollectionResult(
                vm_id=t.vm_id,
                succeeded=True,
                collected_data=_full_state(t.host),
                completed_at=datetime.now(timezone.utc),
            )
            if on_vm_complete:
                on_vm_complete(result)
        return []

    monkeypatch.setattr(
        "app.core.collection.wave_jobs.run_collection_batch", second_run
    )
    retry = client.post(f"/api/baseline-runs/{original_run_id}/retry-failed")
    assert retry.status_code == 202, retry.text
    new_run_id = retry.json()["baseline_run_id"]
    assert new_run_id != original_run_id
    assert len(seen_vm_ids) == 1
    new_detail = client.get(f"/api/baseline-runs/{new_run_id}").json()
    assert new_detail["total_vms"] == 1
    assert new_detail["captured_vms"] == 1


# ---------------------------------------------------------------------------
# Validation kick-off + read
# ---------------------------------------------------------------------------
def _seed_completed_baseline(client, plan_id, key_id, monkeypatch):
    async def fake_batch(*, targets, engine, max_concurrency, on_vm_complete):
        for t in targets:
            result = CollectionResult(
                vm_id=t.vm_id,
                succeeded=True,
                collected_data=_full_state(t.host),
                completed_at=datetime.now(timezone.utc),
            )
            if on_vm_complete:
                on_vm_complete(result)
        return []

    monkeypatch.setattr(
        "app.core.collection.wave_jobs.run_collection_batch", fake_batch
    )
    r = client.post(
        f"/api/plans/{plan_id}/waves/1/baseline", json={"ssh_key_id": key_id}
    )
    return r.json()["baseline_run_id"]


def test_validation_rejects_when_no_baseline_exists(
    isolated_key_dir, client, plan_with_wave, db_session
):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="v-key")
    r = client.post(
        f"/api/plans/{plan.id}/waves/1/validate",
        json={"ssh_key_id": key.id},
    )
    assert r.status_code == 422
    assert "no completed baseline" in r.json()["detail"].lower()


def test_validation_produces_pass_when_state_matches_baseline(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="vk")
    key_id = key.id

    _seed_completed_baseline(client, plan.id, key_id, monkeypatch)
    # Validation: same state as baseline → pass.
    r = client.post(
        f"/api/plans/{plan.id}/waves/1/validate", json={"ssh_key_id": key_id}
    )
    assert r.status_code == 202, r.text
    run_id = r.json()["validation_run_id"]
    detail = client.get(f"/api/validation-runs/{run_id}").json()
    assert detail["status"] == "completed"
    assert detail["passed_vms"] == 3
    assert detail["warned_vms"] == 0
    assert detail["failed_vms"] == 0
    for v in detail["validations"]:
        assert v["verdict"] == "pass"
        assert v["diff_result"] is not None


def test_validation_produces_fail_when_service_regression(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="vk2")
    key_id = key.id
    _seed_completed_baseline(client, plan.id, key_id, monkeypatch)

    # Validation collection: ssh service is failed → fail verdict.
    async def fake_batch(*, targets, engine, max_concurrency, on_vm_complete):
        for t in targets:
            state = _full_state(t.host)
            state["services"] = [
                {"unit": "sshd.service", "active": "failed", "sub": "failed"}
            ]
            result = CollectionResult(
                vm_id=t.vm_id,
                succeeded=True,
                collected_data=state,
                completed_at=datetime.now(timezone.utc),
            )
            if on_vm_complete:
                on_vm_complete(result)
        return []

    monkeypatch.setattr(
        "app.core.collection.wave_jobs.run_collection_batch", fake_batch
    )
    r = client.post(
        f"/api/plans/{plan.id}/waves/1/validate", json={"ssh_key_id": key_id}
    )
    run_id = r.json()["validation_run_id"]
    detail = client.get(f"/api/validation-runs/{run_id}").json()
    assert detail["failed_vms"] == 3
    assert detail["passed_vms"] == 0
    for v in detail["validations"]:
        assert v["verdict"] == "fail"


def test_validation_unreachable_when_collection_fails(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="vk3")
    key_id = key.id
    _seed_completed_baseline(client, plan.id, key_id, monkeypatch)

    async def fake_batch(*, targets, engine, max_concurrency, on_vm_complete):
        for t in targets:
            result = CollectionResult(
                vm_id=t.vm_id,
                succeeded=False,
                failure_category="unreachable",
                failure_detail="Connection refused",
                completed_at=datetime.now(timezone.utc),
            )
            if on_vm_complete:
                on_vm_complete(result)
        return []

    monkeypatch.setattr(
        "app.core.collection.wave_jobs.run_collection_batch", fake_batch
    )
    r = client.post(
        f"/api/plans/{plan.id}/waves/1/validate", json={"ssh_key_id": key_id}
    )
    run_id = r.json()["validation_run_id"]
    detail = client.get(f"/api/validation-runs/{run_id}").json()
    assert detail["unreachable_vms"] == 3


# ---------------------------------------------------------------------------
# Day-2 revoke
# ---------------------------------------------------------------------------
def test_revoke_key_requires_completed_clean_validation_by_default(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="rev")

    # No validation yet — must 422 without force.
    r = client.post(
        f"/api/plans/{plan.id}/waves/1/revoke-validation-key",
        json={"ssh_key_id": key.id},
    )
    assert r.status_code == 422
    assert "no completed validation" in r.json()["detail"].lower()


def test_revoke_key_force_bypasses_gate(
    isolated_key_dir, client, plan_with_wave, db_session, monkeypatch
):
    plan, _ = plan_with_wave
    key = ssh_key_service.generate_keypair(db_session, name="rev2")

    # Stub the revocation helper so we don't try real SSH.
    monkeypatch.setattr(
        "app.services.ssh_key_service._revoke_pubkey_on_host",
        lambda **kw: None,
    )
    r = client.post(
        f"/api/plans/{plan.id}/waves/1/revoke-validation-key",
        json={"ssh_key_id": key.id, "force": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 3
    assert body["succeeded"] == 3
    assert body["failed"] == 0
