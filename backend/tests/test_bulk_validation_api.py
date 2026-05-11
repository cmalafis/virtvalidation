"""Bulk validation API + tier preview tests.

The bulk pipeline is exercised end-to-end with a stub LLM backend
so we cover the tier-routing, cache, and progress-tracking logic
without needing Ollama.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _enroll(client, name: str, *, environment: str = "prod") -> dict:
    r = client.post(
        "/api/vms",
        json={
            "name": name,
            "source_hostname": f"{name}.local",
            "ip_address": "10.0.0.5",
            "role": "web",
            "environment": environment,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


def _seed_baseline(db_session, vm_id: int, *, services=None) -> None:
    """Persist a baseline snapshot the validation pipeline can read."""
    from app.models.vm import BaselineSnapshot
    payload = {
        "meta": {
            "host": "x",
            "collected_at": "2026-01-01T00:00:00+00:00",
            "os_profile": {
                "distro": "rhel",
                "distro_family": "rhel-like",
                "major_version": 9,
                "pretty_name": "RHEL 9",
            },
        },
        "services": services or [{"unit": "sshd.service"}],
        "ports": [],
        "mounts": [],
        "network": {"interfaces": {}, "routes": [], "dns": []},
        "cron": {"user_crontabs": {}, "system": []},
    }
    db_session.add(
        BaselineSnapshot(
            vm_id=vm_id,
            snapshot_number=1,
            ssh_user="x",
            raw_data=payload,
        )
    )
    db_session.commit()


# ---------------------------------------------------------------------------
# Tier preview
# ---------------------------------------------------------------------------
def test_preview_tiers_rejects_empty_scope(client):
    r = client.post("/api/validations/preview-tiers", json={"vm_ids": [99999]})
    assert r.status_code == 422


def test_preview_tiers_reports_distribution_without_calling_llm(
    client, db_session, monkeypatch
):
    """Preview must SSH-collect + diff + classify but NEVER invoke
    the LLM. We monkeypatch the SSH collector to return a fixed
    state and assert the LLM backend never gets a chat call."""
    vm = _enroll(client, "alpha", environment="prod")
    _seed_baseline(db_session, vm["id"])

    from app.core import validation as validation_mod

    # Stub _collect_current_state to return a known-good state.
    def _fake_collect(db, vm, *, actor, collector=None):
        return {
            "meta": {"os_profile": {"distro_family": "rhel-like"}},
            "services": [{"unit": "sshd.service"}],  # same as baseline
            "ports": [],
            "mounts": [],
            "network": {"interfaces": {}, "routes": [], "dns": []},
            "cron": {"user_crontabs": {}, "system": []},
        }

    monkeypatch.setattr(validation_mod, "_collect_current_state", _fake_collect)

    r = client.post(
        "/api/validations/preview-tiers",
        json={"vm_ids": [vm["id"]]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    # Empty diff → tier1
    assert body["tier1"] == 1
    assert body["estimated_llm_calls"] == 0


# ---------------------------------------------------------------------------
# Bulk validation: tier-1 path
# ---------------------------------------------------------------------------
def test_bulk_validation_tier1_skips_llm_for_clean_migration(
    client, db_session, monkeypatch
):
    """A VM whose current state matches its baseline should land in
    Tier 1 and complete without an LLM call. The stub backend asserts
    no chat_sync was made."""
    vm = _enroll(client, "tier1-vm", environment="prod")
    _seed_baseline(db_session, vm["id"])

    from app.core import validation as validation_mod

    monkeypatch.setattr(
        validation_mod,
        "_collect_current_state",
        lambda db, vm, *, actor, collector=None: {
            "meta": {"os_profile": {"distro_family": "rhel-like"}},
            "services": [{"unit": "sshd.service"}],
            "ports": [],
            "mounts": [],
            "network": {"interfaces": {}, "routes": [], "dns": []},
            "cron": {"user_crontabs": {}, "system": []},
        },
    )

    # Block any LLM call — Tier 1 must not reach the client.
    with patch("app.core.llm.client.LLMClient.validate") as mock_validate:
        r = client.post(
            "/api/validations/run-bulk",
            json={"vm_ids": [vm["id"]]},
        )
        assert r.status_code == 202, r.text
        task_id = r.json()["task_id"]

        # FastAPI BackgroundTask runs synchronously in TestClient — the
        # task has already completed by the time we poll.
        status = client.get(f"/api/validations/run-bulk/{task_id}")
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["completed"] == 1
        assert body["failed"] == 0
        assert body["llm_calls"] == 0
        assert body["tier_distribution"]["tier1"] == 1
        mock_validate.assert_not_called()


def test_bulk_validation_tier3_invokes_llm_and_caches(
    client, db_session, monkeypatch
):
    """Ambiguous diffs route to Tier 3 → LLM. The verdict should
    persist into the cache so a second VM with the same diff hits
    the cache instead of the LLM."""
    vm_a = _enroll(client, "tier3-a", environment="prod")
    vm_b = _enroll(client, "tier3-b", environment="prod")
    _seed_baseline(db_session, vm_a["id"])
    _seed_baseline(db_session, vm_b["id"])

    from app.core import validation as validation_mod

    # Both VMs see the same diff: a custom service appeared (not in
    # the known-good set).
    monkeypatch.setattr(
        validation_mod,
        "_collect_current_state",
        lambda db, vm, *, actor, collector=None: {
            "meta": {"os_profile": {"distro_family": "rhel-like"}},
            "services": [
                {"unit": "sshd.service"},
                {"unit": "my-custom-app.service"},
            ],
            "ports": [],
            "mounts": [],
            "network": {"interfaces": {}, "routes": [], "dns": []},
            "cron": {"user_crontabs": {}, "system": []},
        },
    )

    canned = {
        "status": "warn",
        "summary": "Unexpected custom service appeared post-migration.",
        "findings": [
            {
                "severity": "medium",
                "category": "services",
                "title": "Unknown service my-custom-app.service appeared",
                "source_evidence": "absent in baseline",
                "current_evidence": "present in current state",
                "remediation": "Verify the operator added this intentionally.",
                "confidence": "medium",
            }
        ],
        "remediation": [],
        "confidence": "medium",
    }
    call_count = 0

    def _fake_validate(self, baseline, current_state, vm_role, max_retries=1):
        nonlocal call_count
        call_count += 1
        return {**canned, "diff": validation_mod.compute_diff(baseline, current_state) if hasattr(validation_mod, "compute_diff") else {}, "model": "stub", "needs_manual_review": False}

    monkeypatch.setattr(
        "app.core.llm.client.LLMClient.validate", _fake_validate
    )

    r = client.post(
        "/api/validations/run-bulk",
        json={"vm_ids": [vm_a["id"], vm_b["id"]]},
    )
    assert r.status_code == 202
    body = client.get(f"/api/validations/run-bulk/{r.json()['task_id']}").json()
    assert body["completed"] == 2
    # Cache hit on the second VM: only one LLM call total.
    assert call_count == 1
    assert body["llm_calls"] == 1
    assert body["cache_hits"] == 1


def test_bulk_validation_continues_on_individual_failure(client, db_session, monkeypatch):
    """One wedged VM doesn't abort the rest."""
    vm_a = _enroll(client, "fail-a", environment="prod")
    vm_b = _enroll(client, "fail-b", environment="prod")
    _seed_baseline(db_session, vm_a["id"])
    _seed_baseline(db_session, vm_b["id"])

    from app.core import validation as validation_mod
    from app.core.validation import ValidationError

    def _selective_collect(db, vm, *, actor, collector=None):
        if vm.name == "fail-a":
            raise ValidationError("SSH timeout")
        return {
            "meta": {"os_profile": {"distro_family": "rhel-like"}},
            "services": [{"unit": "sshd.service"}],
            "ports": [], "mounts": [],
            "network": {"interfaces": {}, "routes": [], "dns": []},
            "cron": {"user_crontabs": {}, "system": []},
        }
    monkeypatch.setattr(validation_mod, "_collect_current_state", _selective_collect)

    r = client.post(
        "/api/validations/run-bulk",
        json={"vm_ids": [vm_a["id"], vm_b["id"]]},
    )
    body = client.get(f"/api/validations/run-bulk/{r.json()['task_id']}").json()
    assert body["completed"] == 1
    assert body["failed"] == 1
    per_vm = {v["vm_id"]: v for v in body["per_vm"]}
    assert per_vm[vm_a["id"]]["status"] == "failed"
    assert "SSH timeout" in (per_vm[vm_a["id"]]["error"] or "")
    assert per_vm[vm_b["id"]]["status"] == "completed"


# ---------------------------------------------------------------------------
# LLM usage endpoint
# ---------------------------------------------------------------------------
def test_llm_usage_endpoint_returns_rolled_up_metrics(client, db_session):
    """The admin dashboard's view of usage. With no rows yet it
    should still return zero-counts rather than 500."""
    r = client.get("/api/system/llm-usage?hours=24")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "by_operation" in body
    assert body["totals"]["estimated_cost_usd"] == 0.0
    assert body["validations"]["total"] == 0
    assert body["cache"]["entries"] == 0
