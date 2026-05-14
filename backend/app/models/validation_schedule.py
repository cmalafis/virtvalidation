"""Scheduled validation runs.

Federal customers re-validate inventory on a cadence — "every prod VM
every 24 hours", "every VM in wave 3 30 minutes after the wave
completes". The schedule rows here are the source of truth; the
APScheduler integration in :mod:`app.core.scheduler` reads them at
startup and fires the corresponding bulk-validation task on the
configured cron expression.

We deliberately don't run scheduled jobs in-process during pytest —
the schedule store is just a model, the scheduling integration
gates on a config flag.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType


class ScheduleStatus(str, enum.Enum):
    enabled = "enabled"
    paused = "paused"
    disabled = "disabled"


class ValidationSchedule(Base):
    __tablename__ = "validation_schedules"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)

    # Cron expression (5-field standard: m h dom mon dow). APScheduler
    # parses this on schedule activation. We store the raw string so
    # operators can edit it via the UI without us doing a separate
    # composer.
    cron_expression: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(
        String(64), default="UTC", server_default="UTC", nullable=False
    )

    # Scope is the same shape as PlanScopeFilter — vm_ids /
    # source_vcenter_id / environment / application_hint. The
    # scheduler resolves this at fire time so newly-added VMs get
    # included automatically.
    scope: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    status: Mapped[ScheduleStatus] = mapped_column(
        Enum(ScheduleStatus, name="validation_schedule_status"),
        default=ScheduleStatus.enabled,
        server_default=ScheduleStatus.enabled.value,
        nullable=False,
    )

    # Operator notes. Federal reviewers want to see why a schedule
    # exists ("DHA Phase 1 prod fleet — required by COMPL-22-014").
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Bookkeeping.
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When True the scheduler skips this row even if status=enabled.
    # Used by the "pause all" runtime override on the admin page.
    runtime_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )

    created_by_actor: Mapped[str] = mapped_column(
        String(255), default="user", server_default="user", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (Index("ix_validation_schedules_status", "status"),)
