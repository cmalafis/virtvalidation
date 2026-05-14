"""LLM usage telemetry.

One row per LLM call (validation, plan-generation per-chunk call,
design-review analyzer, mapping-suggestion call). The admin dashboard
rolls these up into "Last 24h: 47 validations, 12 LLM calls" + cost
projections.

Per-token cost is configured on the backend side (Settings) and the
materialized cost is computed at query time from token counts. We
don't persist a "cost" column because the rate changes when an
operator updates the pricing.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class LLMUsage(Base):
    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Which operation drove the call. Free-form because new
    # consumers will land over time (target discovery, mapping
    # suggestions, plan review, etc.) and we don't want an enum
    # migration per addition.
    operation: Mapped[str] = mapped_column(String(64), nullable=False)

    backend_type: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    # Token counts when the backend reported them; -1 when unknown.
    input_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    output_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )

    # Wall-clock latency in milliseconds. Helps spot when a backend
    # is in a degraded state (long latency without errors).
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # Optional linkage to the entity that triggered the call so
    # operators can drill down from "spike in usage" to the specific
    # VM / plan / chunk.
    resource_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    vm_id: Mapped[int | None] = mapped_column(
        ForeignKey("vms.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_llm_usage_op_time", "operation", "created_at"),
        Index("ix_llm_usage_time", "created_at"),
    )
