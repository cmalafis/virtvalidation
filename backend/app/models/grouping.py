"""Hierarchical migration planning data model.

The scale-aware planner produces output at three levels (categorize →
strategy → wave detail). The schema here lets each level persist its
output independently so the UI can render progress as the LLM works.

  - :class:`VMGroup`        — Level 1 output. A VM can belong to many
    groups simultaneously (e.g. application=epic-emr,
    environment=prod, business_unit=hospital-east).
  - :class:`VMGroupMember`  — many-to-many association between VMs and
    groups, with a ``confidence`` field so reviewers can spot the
    LLM's lower-conviction assignments.
  - :class:`MigrationProgram` and :class:`Campaign` — Level 2/3 output
    placeholders. The full hierarchical planner is in flight; the
    tables exist so the migration to a streaming-aware UI doesn't
    require a schema break later.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType


class GroupKind(str, enum.Enum):
    """Logical grouping axes Level 1 categorization emits.

    The same VM ends up in one row of each kind, so
    ``WHERE kind=GroupKind.application`` returns every application
    grouping the LLM identified.
    """

    application = "application"
    environment = "environment"
    business_unit = "business_unit"
    # Free-form catch-all for groupings the LLM identifies that don't
    # fit the canonical axes above (e.g., "regulatory-hipaa" tier).
    other = "other"


class VMGroup(Base):
    """Logical group of VMs identified by Level 1 categorization.

    Scoped to a vCenter source — groups don't cross vCenter boundaries
    so federal customers running multiple isolated environments don't
    accidentally see cross-classification associations.
    """

    __tablename__ = "vm_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_vcenter_id: Mapped[int] = mapped_column(
        ForeignKey("vcenter_sources.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[GroupKind] = mapped_column(
        Enum(GroupKind, name="vm_group_kind"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Audit trail for the LLM run that produced this group; helps when
    # comparing two runs of the same vCenter to see what shifted.
    created_by_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    members: Mapped[list["VMGroupMember"]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        # Same logical group name can only exist once per (vcenter, kind).
        UniqueConstraint(
            "source_vcenter_id", "kind", "name", name="uq_group_vcenter_kind_name"
        ),
        Index("ix_groups_vcenter_kind", "source_vcenter_id", "kind"),
    )


class VMGroupMember(Base):
    """Many-to-many: which VMs belong to which group.

    Stored as a separate row (not a JSON array on VMGroup) so:
      - Adding a single VM to a group is a one-row insert, not a JSON
        rewrite of the entire membership list.
      - The diff engine for delta uploads can compare memberships
        cheaply (set ops on the VM ids).
      - PostgreSQL indexes the membership table efficiently.
    """

    __tablename__ = "vm_group_members"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("vm_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vm_id: Mapped[int] = mapped_column(
        ForeignKey("vms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 0.0 – 1.0; lets reviewers triage the LLM's lower-conviction
    # assignments without re-running the categorizer.
    confidence: Mapped[float] = mapped_column(
        Float, nullable=False, default=1.0, server_default="1.0"
    )
    # Free-form rationale the LLM emitted alongside the assignment.
    rationale: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    group: Mapped[VMGroup] = relationship(back_populates="members")

    __table_args__ = (
        UniqueConstraint("group_id", "vm_id", name="uq_member_group_vm"),
    )


# ---------------------------------------------------------------------------
# Migration program / campaign tables — Level 2 / Level 3 placeholders.
# Schema is shipped now so the in-flight hierarchical planner can persist
# results without a follow-up migration. Full integration is deferred —
# see docs/SCALE.md "Deferred work" section.
# ---------------------------------------------------------------------------
class MigrationProgram(Base):
    """A migration program is the top-level container an operator runs.

    A customer with multiple concurrent migrations (East Coast, West
    Coast) creates one Program per effort — same data model, different
    timeline, different team. Level 2 LLM output lands here.

    Today this is a thin record — the full Program / Campaign / Wave
    hierarchy is the next major increment of the planner. The table
    exists so the data layer is ready when the UI catches up.
    """

    __tablename__ = "migration_programs"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # vCenter scope — null means "every vCenter". Most programs scope
    # to a single vCenter (or a list of vCenters serialized to JSON).
    source_vcenter_ids: Mapped[list[int]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    # Level 2 strategy output — campaigns + sequencing. Stored as JSON
    # for now; the full Campaign / Wave normalization comes with the
    # streaming-UI work.
    strategy: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
