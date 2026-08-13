"""Tests for the read-only inference-log API (pagination, filters, stats)."""

from __future__ import annotations

import pytest

from app.core import db as _db_module
from app.models.inference_log import InferenceLog


@pytest.fixture
def seed_logs(client):
    """Insert a spread of inference rows through the test SessionLocal so the
    API (which uses the same engine) reads them back."""
    session = _db_module.SessionLocal()
    try:
        rows = [
            InferenceLog(
                operation="validation",
                backend_type="mock",
                model="m",
                method="llm",
                verdict="pass",
                vm_id=1,
            ),
            InferenceLog(
                operation="validation",
                backend_type="mock",
                model="m",
                method="llm_retry_1",
                verdict="warn",
                vm_id=2,
            ),
            InferenceLog(
                operation="validation",
                backend_type="maas",
                model="g",
                method="manual_review",
                verdict="warn",
                vm_id=3,
            ),
            InferenceLog(
                operation="wave_annotation",
                backend_type="mock",
                model="m",
                method="mechanical_fallback",
                resource_type="plan",
                resource_id=9,
            ),
            InferenceLog(
                operation="wave_annotation",
                backend_type="mock",
                model="m",
                method="llm",
                resource_type="plan",
                resource_id=9,
            ),
        ]
        session.add_all(rows)
        session.commit()
    finally:
        session.close()
    return client


def test_list_returns_wrapped_pagination_shape(seed_logs):
    resp = seed_logs.get("/api/inference-logs")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "total", "skip", "limit"}
    assert body["total"] == 5
    assert len(body["items"]) == 5


def test_list_filters_by_operation_and_method(seed_logs):
    resp = seed_logs.get("/api/inference-logs", params={"operation": "validation"})
    assert resp.json()["total"] == 3

    resp = seed_logs.get("/api/inference-logs", params={"method": ["llm", "llm_retry_1"]})
    assert resp.json()["total"] == 3

    resp = seed_logs.get("/api/inference-logs", params={"vm_id": 2})
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["method"] == "llm_retry_1"


def test_list_pagination_limits(seed_logs):
    resp = seed_logs.get("/api/inference-logs", params={"limit": 2, "skip": 0})
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_stats_rolls_up_by_method_and_operation(seed_logs):
    resp = seed_logs.get("/api/inference-logs/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert body["by_method"]["llm"] == 2
    assert body["by_method"]["mechanical_fallback"] == 1
    assert body["by_operation"]["validation"] == 3
    assert body["by_operation"]["wave_annotation"] == 2
