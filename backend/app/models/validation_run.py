"""Wave-scoped validation runs.

Mirrors :mod:`app.models.baseline_run`: a ``ValidationRun`` is one async
job that collects post-migration data for every VM in a single wave and
diffs it against the wave's :class:`Baseline`.

The diff is fully deterministic Python (see
``app.core.collection.diff``) — this session adds zero LLM calls. The
existing per-VM tier-aware validation in ``app.core.validation`` is
*untouched* and continues to operate.

Per-VM verdicts collapse to per-dimension diffs in
:attr:`VMValidation.diff_result`. Overall verdict is the worst per-
dimension verdict; an unreachable VM short-circuits to
:attr:`VMValidationVerdict.unreachable` with no diff at all.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


class ValidationRunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class VMValidationVerdict(str, enum.Enum):
    """Per-VM validation verdict.

    Names differ from values (``passed = "pass"`` etc.) so this enum needs
    ``values_callable`` on the SQLAlchemy column AND ``use_enum_values``
    on the Pydantic schema (see CLAUDE.md "Enum I/O rules"). API output
    is the VALUE (``"pass"``, ``"warn"``, ``"fail"``, ``"unreachable"``).
    """

    passed = "pass"
    warned = "warn"
    failed = "fail"
    unreachable = "unreachable"


class ValidationRun(Base):
    __tablename__ = "validation_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("migration_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    wave_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ssh_key_id: Mapped[int] = mapped_column(
        ForeignKey("ssh_keys.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[ValidationRunStatus] = mapped_column(
        Enum(ValidationRunStatus, name="validation_run_status"),
        default=ValidationRunStatus.pending,
        server_default=ValidationRunStatus.pending.value,
        nullable=False,
        index=True,
    )
    total_vms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    passed_vms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    warned_vms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    failed_vms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    unreachable_vms: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    progress_message: Mapped[str | None] = mapped_column(String(256), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_validation_runs_plan_wave", "plan_id", "wave_number"),)


class VMValidation(Base):
    __tablename__ = "vm_validations"

    id: Mapped[int] = mapped_column(primary_key=True)
    validation_run_id: Mapped[int] = mapped_column(
        ForeignKey("validation_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    vm_id: Mapped[int] = mapped_column(
        ForeignKey("vms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SET NULL: a baseline row may be deleted (e.g. plan teardown) while the
    # validation result is kept for audit. The diff is still meaningful even
    # if the baseline it was computed from is gone.
    baseline_id: Mapped[int | None] = mapped_column(
        ForeignKey("baselines.id", ondelete="SET NULL"),
        nullable=True,
    )
    verdict: Mapped[VMValidationVerdict] = mapped_column(
        Enum(
            VMValidationVerdict,
            name="vm_validation_verdict",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=VMValidationVerdict.unreachable,
        server_default=VMValidationVerdict.unreachable.value,
        nullable=False,
        index=True,
    )
    collected_data: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # Structured diff produced by ``app.core.collection.diff.diff_collection``.
    # Shape: ``{overall, dimensions: [{name, verdict, baseline, current,
    # note}, ...]}``.
    diff_result: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    # True when post-migration host key differs from the baseline's. Always
    # informational, never fatal — a migrated VM is expected to land on a
    # new OCP-Virt host with a different host key.
    host_key_changed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (Index("ix_vm_validations_run_verdict", "validation_run_id", "verdict"),)
