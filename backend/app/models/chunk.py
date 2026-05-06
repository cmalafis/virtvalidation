"""Persisted plan chunks.

A :class:`MigrationPlan` produced by the hierarchical planner is a
collection of chunks plus assembled waves. The chunks are derived
deterministically from the inventory, but persisting them lets the UI
render a "chunk navigation" view weeks later without re-running the
chunker, and lets federal reviewers audit *why* the chunker drew the
boundaries it did.

The waves themselves still live on ``MigrationPlan.waves`` (a JSON
list) so the existing report / MTV-YAML / move-vm endpoints don't
need to change. Each wave row carries an optional ``chunk_id`` so
the UI can group waves under their owning chunk.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType


class PlanChunk(Base):
    __tablename__ = "plan_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("migration_plans.id", ondelete="CASCADE"), nullable=False
    )

    # The uuid the chunker stamps. ``id`` is the DB surrogate key;
    # ``chunk_id`` is the externally-visible identifier the planner
    # uses for cross-chunk dependency edges.
    chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Display order in the plan view's chunk navigation. The chunker
    # decides the order; we persist it so the UI doesn't have to
    # reproduce ``_ordering_key`` on every render.
    sequence_index: Mapped[int] = mapped_column(Integer, nullable=False)

    # Operator-facing fields.
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    reason_for_chunk: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Structured fields. partition_key + sub_key together describe how
    # the chunker selected this chunk; hints carries the soft signals
    # the per-chunk LLM call was given as context.
    partition_key: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    sub_key: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    hints: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    vm_ids: Mapped[list[int]] = mapped_column(JSONType, nullable=False, default=list)
    sequence_dependencies: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )

    # Per-chunk LLM output. The orchestrator copies the chunk-level
    # rationale + risk + waves here so federal reviewers can audit
    # the per-chunk reasoning independently of the assembled plan.
    chunk_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    wave_numbers: Mapped[list[int]] = mapped_column(
        JSONType, nullable=False, default=list
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plan = relationship("MigrationPlan", foreign_keys=[plan_id])

    __table_args__ = (
        Index("ix_plan_chunks_plan_id", "plan_id"),
        Index("ix_plan_chunks_plan_seq", "plan_id", "sequence_index"),
    )
