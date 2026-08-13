"""Full LLM inference capture — one row per model call, input and output.

Distinct from :class:`app.models.llm_usage.LLMUsage`, which is deliberately
lean telemetry (token counts + latency for the cost dashboard). This table is
the **auditable record of what the agent asked the model and what it got
back** — the prerequisite for TrustyAI explainability and for answering "why
did the agent reach this verdict against this production VM?".

It stores the exact prompt messages sent, the raw response text, the
fallback ``method`` taken (``llm`` / ``llm_retry_N`` / ``mechanical_fallback``
/ ``mechanical_fallback_auth`` / ``mechanical_fallback_guardrail``), and any
guardrail ``detections`` the Guardrails Orchestrator returned. Append-only by
convention — there are no update/delete endpoints, matching the audit-log
posture federal customers require.

Written from the orchestrator layer (validation, wave annotation) where a DB
session is in scope — backends stay pure transport and never touch the DB.
See :func:`app.core.llm.inference_log.record_inference`.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class InferenceLog(Base):
    __tablename__ = "inference_logs"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Which flow drove the call. Free-form like LLMUsage.operation so new
    # consumers (network review, mapping suggestion) don't need a migration.
    operation: Mapped[str] = mapped_column(String(64), nullable=False)

    backend_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # The fallback / success path taken, mirroring the wave-annotation
    # ``method`` vocabulary so operators can correlate audit rows with the
    # validate-retry-fallback discipline:
    #   llm | llm_retry_N | mechanical_fallback | mechanical_fallback_auth |
    #   mechanical_fallback_guardrail | manual_review
    method: Mapped[str] = mapped_column(String(48), nullable=False, default="llm")

    # Exact OpenAI-style message list sent to the backend (system + user, plus
    # any retry turns). JSON so the structure round-trips for replay/audit.
    input_messages: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Raw response text the model returned (pre-parse). Text, not JSON — it may
    # be malformed JSON on a rejected attempt, which is itself worth auditing.
    output_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Guardrail detections returned by the TrustyAI Guardrails Orchestrator,
    # when that backend is active. Null for non-guardrailed backends.
    detections: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # The parsed outcome where the flow produces one (e.g. validation verdict
    # "pass"/"warn"/"fail"). Null when not applicable.
    verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)

    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # Optional linkage to the entity that triggered the call so operators can
    # drill from a finding back to the exact inference that produced it.
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vm_id: Mapped[int | None] = mapped_column(
        ForeignKey("vms.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_inference_logs_op_time", "operation", "created_at"),
        Index("ix_inference_logs_time", "created_at"),
        Index("ix_inference_logs_method", "method"),
    )
