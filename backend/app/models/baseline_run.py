"""Wave-scoped baseline capture runs.

A ``BaselineRun`` is one async job that captures a structured pre-migration
sample for every VM in a single wave. Wave is identified by the composite
``(plan_id, wave_number)`` — waves are still JSON inside
``MigrationPlan.waves`` (no first-class Wave table) so runs reference them
positionally.

Each VM in the wave gets one ``Baseline`` row. Per-VM failures are tracked
on the Baseline row, not on the run — a run with 970 captured / 30 failed
is a *completed* run, not a failed one (partial success is valid).

``Baseline.collected_data`` is the structured Pass-1 output (see
``app.core.collection.collector_spec``). The ``probe_catalog_version`` +
``probes_run`` fields record the spec identity so a later LLM-directed
probing session can record workload-specific probe selections in the same
fields, and so validation can re-run the *exact* same probe set the
baseline used — no schema migration needed in that session.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType


class BaselineRunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    # "completed" means every VM reached a terminal state. Some VMs may have
    # failed individually; the run is still completed (partial success is a
    # valid outcome).
    completed = "completed"
    # "failed" is reserved for the run itself failing to orchestrate (e.g.
    # the background task crashed before reaching the per-VM loop). Per-VM
    # failures do NOT set this.
    failed = "failed"


class VMCollectionStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    captured = "captured"
    failed = "failed"


class BaselineRun(Base):
    __tablename__ = "baseline_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Composite wave reference. No Wave table — waves live as JSON on
    # MigrationPlan.waves. The handler resolves vm_ids from
    # plan.waves[wave_number - 1].
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("migration_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    wave_number: Mapped[int] = mapped_column(Integer, nullable=False)
    # RESTRICT so a key can't be deleted while a run references it; the
    # operator must retire keys explicitly.
    ssh_key_id: Mapped[int] = mapped_column(
        ForeignKey("ssh_keys.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[BaselineRunStatus] = mapped_column(
        Enum(BaselineRunStatus, name="baseline_run_status"),
        default=BaselineRunStatus.pending,
        server_default=BaselineRunStatus.pending.value,
        nullable=False,
        index=True,
    )
    total_vms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    captured_vms: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    failed_vms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    progress_message: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Operator authorization for running against production / classified hosts.
    # Populated by the kick-off endpoint when the authorization gate applies;
    # null for runs that didn't require it. Provides the audit answer to "who
    # authorized SSHing into these production servers, and why?".
    authorized_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    authorization_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_baseline_runs_plan_wave", "plan_id", "wave_number"),)


class Baseline(Base):
    """Per-VM baseline sample owned by a :class:`BaselineRun`.

    Designed as a sample collection: a VM can accumulate many Baseline rows
    over time (multi-day collection in a future session; v1 captures one per
    run). The structured collection data lives in :attr:`collected_data` as
    JSON — extensible with LLM-probe data later without a schema change.
    """

    __tablename__ = "baselines"

    id: Mapped[int] = mapped_column(primary_key=True)
    baseline_run_id: Mapped[int] = mapped_column(
        ForeignKey("baseline_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vm_id: Mapped[int] = mapped_column(
        ForeignKey("vms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[VMCollectionStatus] = mapped_column(
        Enum(VMCollectionStatus, name="vm_collection_status"),
        default=VMCollectionStatus.pending,
        server_default=VMCollectionStatus.pending.value,
        nullable=False,
        index=True,
    )
    # Structured Pass-1 data. Shape defined by the collector spec; see
    # ``app.core.collection.collector_spec``.
    collected_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # Identity of the probe catalog that produced this row. In v1 this is
    # always ``"pass1-v1"``. A later session that adds LLM-directed probing
    # will write its own catalog version here and select a probe subset.
    probe_catalog_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    probes_run: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    # Host key fingerprint captured at baseline time. Validation compares
    # against this; a change is informational (post-migration is expected to
    # land on a new host key), never fatal.
    host_key_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    collection_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    collection_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_baselines_run_status", "baseline_run_id", "status"),)
