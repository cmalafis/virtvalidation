"""Tests for full LLM inference capture (InferenceLog + record_inference)."""

from __future__ import annotations

from app.core.llm.inference_log import _MAX_OUTPUT_CHARS, record_inference
from app.models.inference_log import InferenceLog


def test_record_inference_persists_row(db_session):
    record_inference(
        db_session,
        operation="validation",
        backend_type="mock",
        model="llama3:8b",
        method="llm",
        input_messages=[{"role": "system", "content": "x"}, {"role": "user", "content": "y"}],
        output_text='{"status": "pass"}',
        verdict="pass",
        latency_ms=42,
        resource_type="vm",
        resource_id=7,
        vm_id=7,
    )
    rows = db_session.query(InferenceLog).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.operation == "validation"
    assert row.backend_type == "mock"
    assert row.method == "llm"
    assert row.verdict == "pass"
    assert row.input_messages[1]["content"] == "y"
    assert row.output_text == '{"status": "pass"}'
    assert row.vm_id == 7


def test_record_inference_truncates_huge_output(db_session):
    huge = "A" * (_MAX_OUTPUT_CHARS + 5000)
    record_inference(
        db_session,
        operation="validation",
        backend_type="mock",
        model="m",
        output_text=huge,
    )
    row = db_session.query(InferenceLog).one()
    assert len(row.output_text) < len(huge)
    assert "truncated" in row.output_text


def test_record_inference_never_raises_on_db_error(db_session):
    # Passing a bogus type that the column can't store must be swallowed,
    # not propagated — audit capture must never break the LLM flow.
    db_session.close()  # force a broken session
    # Should not raise.
    record_inference(
        db_session,
        operation="validation",
        backend_type="mock",
        model="m",
        output_text="ok",
    )


def test_record_inference_defaults_backend_when_missing(db_session):
    record_inference(db_session, operation="wave_annotation", backend_type="", model="")
    row = db_session.query(InferenceLog).one()
    assert row.backend_type == "unknown"
    assert row.method == "llm"
