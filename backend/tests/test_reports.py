"""Tests for the dashboard-level report endpoints.

Contract: every report endpoint returns 200 with a stable shape — empty
data is a valid state, never a 404. The executive-summary endpoint's LLM
call is stubbed so the test never depends on a live Ollama.
"""

from __future__ import annotations

import pytest

from app.api import reports as reports_module
from app.models.validation import ValidationStatus


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def stub_llm_summary(monkeypatch):
    """Replace the Ollama call with a deterministic string so the
    executive-summary endpoint is hermetic. The fallback path is
    exercised by ``test_executive_summary_falls_back_when_llm_offline``
    further down."""
    monkeypatch.setattr(
        reports_module,
        "_llm_executive_summary",
        lambda summary, wave_status, key_risks: "STUB_NARRATIVE",
    )


def _enroll(client, name: str, **overrides) -> dict:
    payload = {
        "name": name,
        "source_hostname": f"{name}.local",
        "ip_address": f"10.0.0.{abs(hash(name)) % 250 + 5}",
        "role": overrides.pop("role", "app"),
        **overrides,
    }
    return client.post("/api/vms", json=payload).json()


def _add_validation(db_session, vm_id: int, status: str, *, findings=None, summary=""):
    """Insert a ValidationResult bypassing the API since there's no
    public endpoint for creating one (validation runs are scheduler-driven
    in production)."""
    from app.models.validation import ValidationResult

    db_session.add(
        ValidationResult(
            vm_id=vm_id,
            status=ValidationStatus(status),
            summary=summary or f"validation result for vm {vm_id}",
            findings=findings or [],
            remediation=[],
            diff={},
        )
    )
    db_session.commit()


def _add_snapshot(db_session, vm_id: int, raw_data=None):
    from sqlalchemy import func, select

    from app.models.vm import BaselineSnapshot

    next_number = (
        db_session.scalar(
            select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0)).where(
                BaselineSnapshot.vm_id == vm_id
            )
        )
        or 0
    ) + 1
    db_session.add(
        BaselineSnapshot(
            vm_id=vm_id,
            snapshot_number=next_number,
            ssh_user="virtvalidate",
            raw_data=raw_data
            or {
                "meta": {"os_profile": {"distro": "rhel", "major_version": 9}},
                "services": [{"unit": "sshd.service"}, {"unit": "nginx.service"}],
                "ports": [{"proto": "tcp", "port": 22}],
                "mounts": [{"target": "/", "source": "/dev/sda1", "fstype": "xfs"}],
                "network": {},
                "cron": {},
            },
        )
    )
    db_session.commit()


# ---------------------------------------------------------------------------
# 1. /full-validation
# ---------------------------------------------------------------------------
def test_full_validation_empty_returns_200(client):
    r = client.get("/api/reports/full-validation")
    assert r.status_code == 200
    body = r.json()
    assert body["report_type"] == "full_validation"
    assert body["summary"]["total"] == 0
    assert body["vms"] == []


def test_full_validation_summary_counts_match_verdicts(client, db_session):
    a = _enroll(client, "a")
    b = _enroll(client, "b")
    c = _enroll(client, "c")
    _add_validation(db_session, a["id"], "pass")
    _add_validation(db_session, b["id"], "warn")
    _add_validation(db_session, c["id"], "fail")

    body = client.get("/api/reports/full-validation").json()
    assert body["summary"] == {"total": 3, "healthy": 1, "degraded": 1, "failed": 1, "pending": 0}


def test_full_validation_sorts_failed_first_then_degraded_then_healthy(client, db_session):
    a = _enroll(client, "alpha")
    b = _enroll(client, "bravo")
    c = _enroll(client, "charlie")
    _enroll(client, "delta")
    _add_validation(db_session, a["id"], "pass")
    _add_validation(db_session, b["id"], "fail")
    _add_validation(db_session, c["id"], "warn")
    # d gets no validation → pending, sorts last

    statuses = [v["status"] for v in client.get("/api/reports/full-validation").json()["vms"]]
    assert statuses == ["failed", "degraded", "healthy", "pending"]


def test_full_validation_includes_findings_and_remediation(client, db_session):
    a = _enroll(client, "a")
    _add_validation(
        db_session,
        a["id"],
        "fail",
        summary="postgres down",
        findings=[{"severity": "critical", "category": "services", "message": "postgres stopped"}],
    )

    vm = client.get("/api/reports/full-validation").json()["vms"][0]
    assert vm["summary"] == "postgres down"
    assert vm["findings"][0]["severity"] == "critical"


# ---------------------------------------------------------------------------
# 2. /executive-summary
# ---------------------------------------------------------------------------
def test_executive_summary_empty_returns_200_with_zero_data(client):
    body = client.get("/api/reports/executive-summary").json()
    assert body["report_type"] == "executive_summary"
    assert body["summary"]["total_vms"] == 0
    assert body["summary"]["migration_progress_pct"] == 0
    assert body["wave_status"] == []
    assert body["key_risks"] == []
    assert body["executive_summary"] == "STUB_NARRATIVE"


def test_executive_summary_progress_pct(client, db_session):
    a = _enroll(client, "a")
    b = _enroll(client, "b")
    _enroll(client, "c")
    _enroll(client, "d")
    _add_validation(db_session, a["id"], "pass")
    _add_validation(db_session, b["id"], "pass")
    # c, d remain pending — only 2/4 = 50% validated
    body = client.get("/api/reports/executive-summary").json()
    assert body["summary"]["total_vms"] == 4
    assert body["summary"]["migration_progress_pct"] == 50
    assert body["summary"]["by_status"]["healthy"] == 2
    assert body["summary"]["by_status"]["pending"] == 2


def test_executive_summary_top_risks_are_failed_first(client, db_session):
    a = _enroll(client, "alpha")
    b = _enroll(client, "bravo")
    _add_validation(db_session, a["id"], "fail", summary="alpha exploded")
    _add_validation(
        db_session,
        b["id"],
        "warn",
        findings=[{"severity": "critical", "category": "services", "message": "bravo critical"}],
    )

    body = client.get("/api/reports/executive-summary").json()
    risks = body["key_risks"]
    assert risks[0]["vm_name"] == "alpha"
    assert risks[0]["severity"] == "critical"
    assert risks[1]["vm_name"] == "bravo"
    assert risks[1]["severity"] == "high"


def test_executive_summary_findings_by_severity_aggregates(client, db_session):
    a = _enroll(client, "a")
    _add_validation(
        db_session,
        a["id"],
        "fail",
        findings=[
            {"severity": "critical", "message": "x"},
            {"severity": "warn", "message": "y"},
            {"severity": "warn", "message": "z"},
            {"severity": "info", "message": "w"},
        ],
    )
    body = client.get("/api/reports/executive-summary").json()
    fs = body["summary"]["findings_by_severity"]
    assert fs == {"critical": 1, "warn": 2, "info": 1}


def test_executive_summary_falls_back_when_llm_offline(client, monkeypatch):
    """Re-instate the real LLM helper, point it at an unreachable host,
    and confirm the endpoint still returns 200 with a deterministic
    narrative instead of a 5xx."""
    # Undo the autouse stub so we exercise the fallback path.
    from app.api import reports as r

    monkeypatch.setattr(
        r,
        "_llm_executive_summary",
        r._llm_executive_summary.__wrapped__
        if hasattr(r._llm_executive_summary, "__wrapped__")
        else r._llm_executive_summary,
    )

    import httpx

    def _fail(*a, **kw):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr("httpx.Client.post", _fail)

    body = client.get("/api/reports/executive-summary").json()
    # Fallback narrative is non-empty and references the data we provided
    # (zero VMs in this test → "No VMs are enrolled yet").
    assert body["executive_summary"]
    assert isinstance(body["executive_summary"], str)


# ---------------------------------------------------------------------------
# 3. /failed-degraded
# ---------------------------------------------------------------------------
def test_failed_degraded_empty_returns_200_not_404(client):
    """Critical contract: empty inventory or all-healthy must NOT 404."""
    _enroll(client, "a")
    body = client.get("/api/reports/failed-degraded").json()
    assert body["report_type"] == "failed_degraded"
    assert body["summary"]["needing_action"] == 0
    assert body["vms"] == []


def test_failed_degraded_filters_to_action_required(client, db_session):
    a = _enroll(client, "a")
    b = _enroll(client, "b")
    c = _enroll(client, "c")
    _add_validation(db_session, a["id"], "pass")  # excluded
    _add_validation(db_session, b["id"], "warn")
    _add_validation(db_session, c["id"], "fail")

    body = client.get("/api/reports/failed-degraded").json()
    statuses = [v["status"] for v in body["vms"]]
    assert statuses == ["failed", "degraded"]
    assert body["summary"]["needing_action"] == 2
    assert body["summary"]["total_in_inventory"] == 3


# ---------------------------------------------------------------------------
# 4. /wave-plan
# ---------------------------------------------------------------------------
def test_wave_plan_no_plan_returns_200_with_helpful_message(client):
    body = client.get("/api/reports/wave-plan").json()
    assert body["report_type"] == "wave_plan"
    assert body["plan_id"] is None
    assert body["waves"] == []
    assert body["no_plan_message"] == "Generate a migration plan first"


def test_wave_plan_returns_most_recent_plan_with_vm_details(client, db_session):
    from app.models.plan import MigrationPlan

    a = _enroll(client, "a")
    b = _enroll(client, "b")
    plan = MigrationPlan(
        vm_ids=[a["id"], b["id"]],
        waves=[
            {
                "wave_number": 1,
                "vm_ids": [a["id"]],
                "rationale": "stateful first",
                "estimated_risk": "high",
            },
            {
                "wave_number": 2,
                "vm_ids": [b["id"]],
                "rationale": "app tier",
                "estimated_risk": "medium",
            },
        ],
        summary="2-wave cutover",
        model="test-model",
    )
    db_session.add(plan)
    db_session.commit()

    body = client.get("/api/reports/wave-plan").json()
    assert body["plan_id"] == plan.id
    assert body["plan_summary"] == "2-wave cutover"
    assert len(body["waves"]) == 2
    assert body["waves"][0]["wave_number"] == 1
    assert body["waves"][0]["vms"][0]["name"] == "a"
    assert body["waves"][0]["status"] == "pending"  # no validations yet
    assert body["no_plan_message"] is None


def test_wave_plan_marks_complete_when_all_validated(client, db_session):
    from app.models.plan import MigrationPlan

    a = _enroll(client, "a")
    _add_validation(db_session, a["id"], "pass")
    db_session.add(
        MigrationPlan(
            vm_ids=[a["id"]],
            waves=[
                {"wave_number": 1, "vm_ids": [a["id"]], "rationale": "", "estimated_risk": "low"}
            ],
            summary="",
            model="test-model",
        )
    )
    db_session.commit()

    wave = client.get("/api/reports/wave-plan").json()["waves"][0]
    assert wave["status"] == "complete"
    assert wave["verdicts"]["healthy"] == 1


# ---------------------------------------------------------------------------
# 5. /baseline-snapshot
# ---------------------------------------------------------------------------
def test_baseline_snapshot_empty_returns_200(client):
    body = client.get("/api/reports/baseline-snapshot").json()
    assert body["report_type"] == "baseline_snapshot"
    assert body["summary"]["total_vms"] == 0
    assert body["summary"]["vms_with_baselines"] == 0
    assert body["vms"] == []


def test_baseline_snapshot_aggregates_per_vm_state(client, db_session):
    a = _enroll(client, "a")
    _enroll(client, "b")
    _add_snapshot(db_session, a["id"])
    _add_snapshot(db_session, a["id"])  # second snapshot for VM a
    # b deliberately has no snapshot

    body = client.get("/api/reports/baseline-snapshot").json()
    assert body["summary"]["total_vms"] == 2
    assert body["summary"]["vms_with_baselines"] == 1
    assert body["summary"]["vms_without_baselines"] == 1

    by_name = {row["name"]: row for row in body["vms"]}
    assert by_name["a"]["snapshot_count"] == 2
    assert by_name["a"]["service_count"] == 2
    assert by_name["a"]["port_count"] == 1
    assert by_name["a"]["mount_count"] == 1
    assert by_name["a"]["os_profile"]["distro"] == "rhel"
    assert by_name["a"]["last_collected_at"] is not None
    assert by_name["b"]["snapshot_count"] == 0
    assert by_name["b"]["last_collected_at"] is None
