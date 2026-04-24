"""Unit tests for app.core.planner — LLM response validation and wave assembly."""

from __future__ import annotations

import json

import pytest

from app.core.planner import MigrationPlanner, PlannerError

# ---------- _parse_plan validation ----------


def test_parse_plan_happy_path():
    raw = json.dumps(
        {
            "summary": "DB first, then apps",
            "waves": [
                {
                    "wave_number": 1,
                    "vm_ids": [1],
                    "rationale": "stateful DB",
                    "estimated_risk": "high",
                },
                {
                    "wave_number": 2,
                    "vm_ids": [2, 3],
                    "rationale": "app tier",
                    "estimated_risk": "medium",
                },
            ],
        }
    )
    result = MigrationPlanner._parse_plan(raw, {1, 2, 3})
    assert result["summary"] == "DB first, then apps"
    assert [w["wave_number"] for w in result["waves"]] == [1, 2]
    assert result["waves"][0]["estimated_risk"] == "high"


def test_parse_plan_sorts_waves_by_number():
    raw = json.dumps(
        {
            "waves": [
                {"wave_number": 3, "vm_ids": [1], "rationale": "", "estimated_risk": "low"},
                {"wave_number": 1, "vm_ids": [2], "rationale": "", "estimated_risk": "low"},
                {"wave_number": 2, "vm_ids": [3], "rationale": "", "estimated_risk": "low"},
            ]
        }
    )
    result = MigrationPlanner._parse_plan(raw, {1, 2, 3})
    assert [w["wave_number"] for w in result["waves"]] == [1, 2, 3]


def test_parse_plan_rejects_unknown_vm_ids():
    raw = json.dumps(
        {
            "waves": [
                {"wave_number": 1, "vm_ids": [99], "rationale": "", "estimated_risk": "low"},
            ]
        }
    )
    with pytest.raises(PlannerError, match="unknown vm_ids"):
        MigrationPlanner._parse_plan(raw, {1, 2})


def test_parse_plan_rejects_duplicate_vm_across_waves():
    raw = json.dumps(
        {
            "waves": [
                {"wave_number": 1, "vm_ids": [1], "rationale": "", "estimated_risk": "low"},
                {"wave_number": 2, "vm_ids": [1, 2], "rationale": "", "estimated_risk": "low"},
            ]
        }
    )
    with pytest.raises(PlannerError, match="multiple waves"):
        MigrationPlanner._parse_plan(raw, {1, 2})


def test_parse_plan_rejects_missing_vms():
    raw = json.dumps(
        {
            "waves": [
                {"wave_number": 1, "vm_ids": [1], "rationale": "", "estimated_risk": "low"},
            ]
        }
    )
    with pytest.raises(PlannerError, match="did not place"):
        MigrationPlanner._parse_plan(raw, {1, 2})


def test_parse_plan_rejects_invalid_risk():
    raw = json.dumps(
        {
            "waves": [
                {"wave_number": 1, "vm_ids": [1, 2], "rationale": "", "estimated_risk": "extreme"},
            ]
        }
    )
    with pytest.raises(PlannerError, match="estimated_risk"):
        MigrationPlanner._parse_plan(raw, {1, 2})


def test_parse_plan_rejects_empty_waves():
    raw = json.dumps({"waves": []})
    with pytest.raises(PlannerError, match="non-empty"):
        MigrationPlanner._parse_plan(raw, {1})


def test_parse_plan_rejects_non_int_vm_ids():
    raw = json.dumps(
        {
            "waves": [
                {
                    "wave_number": 1,
                    "vm_ids": ["one", "two"],
                    "rationale": "",
                    "estimated_risk": "low",
                },
            ]
        }
    )
    with pytest.raises(PlannerError, match="must all be ints"):
        MigrationPlanner._parse_plan(raw, {1, 2})


def test_parse_plan_rejects_malformed_json():
    with pytest.raises(PlannerError, match="not valid JSON"):
        MigrationPlanner._parse_plan("{not json", {1})


# ---------- plan() end-to-end ----------


def test_plan_rejects_empty_profile_list():
    with pytest.raises(PlannerError, match="zero VMs"):
        MigrationPlanner().plan([])


def test_plan_groups_by_role_via_mocked_ollama(monkeypatch, mock_ollama_plan):
    profiles = [
        {"vm_id": 1, "name": "db-01", "role": "database", "os_family": "rhel", "baseline": {}},
        {"vm_id": 2, "name": "app-01", "role": "app", "os_family": "rhel", "baseline": {}},
        {"vm_id": 3, "name": "app-02", "role": "app", "os_family": "rhel", "baseline": {}},
    ]
    planner = MigrationPlanner()
    monkeypatch.setattr(planner, "_chat", lambda system, user: json.dumps(mock_ollama_plan))

    result = planner.plan(profiles)

    assert len(result["waves"]) == 2
    assert result["waves"][0]["vm_ids"] == [1]  # DB in wave 1
    assert result["waves"][0]["estimated_risk"] == "high"
    assert sorted(result["waves"][1]["vm_ids"]) == [2, 3]
    assert result["waves"][1]["estimated_risk"] == "medium"


def test_plan_prompt_includes_all_profiles(monkeypatch, mock_ollama_plan):
    """Ensure every vm_id the caller supplies is in the prompt body."""
    captured = {}

    def fake_chat(system, user):
        captured["user"] = user
        return json.dumps(mock_ollama_plan)

    profiles = [
        {"vm_id": 1, "name": "db-01", "role": "database", "os_family": "rhel", "baseline": {}},
        {"vm_id": 2, "name": "app-01", "role": "app", "os_family": "rhel", "baseline": {}},
        {"vm_id": 3, "name": "app-02", "role": "app", "os_family": "rhel", "baseline": {}},
    ]
    planner = MigrationPlanner()
    monkeypatch.setattr(planner, "_chat", fake_chat)
    planner.plan(profiles)

    assert "db-01" in captured["user"]
    assert "app-01" in captured["user"]
    assert "app-02" in captured["user"]
    assert '"vm_id": 1' in captured["user"]
