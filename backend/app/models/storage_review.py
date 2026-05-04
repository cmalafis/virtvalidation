"""Storage design review tables.

Sister feature to :mod:`app.models.network_review` — same shape,
different domain. A ``StorageDesignReview`` carries the customer's
notes + proposed OCP-Virt storage YAML and the LLM's structured
findings about gaps between the source VMware datastore layout and
the proposed StorageClass / VolumeSnapshotClass / StorageMap design.

We **reuse** the severity / confidence / triage enums defined in
``network_review`` so the UI and audit-log components don't have
to fork on review kind. Categories are storage-specific because
the issue space is different — performance tier mismatches,
capacity concerns, access-mode (RWX vs RWO) gaps, replication
loss, snapshot capability, multipath policies.

Status transitions: draft → analyzing → completed (or back to
draft on analyze failure). Same persistence model as the network
review — operators can re-run the analysis to refresh findings.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType
from app.models.network_review import (
    FindingConfidence,
    FindingSeverity,
    FindingTriage,
)


class StorageReviewStatus(str, enum.Enum):
    draft = "draft"
    analyzing = "analyzing"
    completed = "completed"
    failed = "failed"


class StorageFindingCategory(str, enum.Enum):
    """Storage-specific issue categories.

    The network review uses ``coverage_gap`` / ``config_mismatch`` /
    ``missing_resource`` — those abstractions don't carry the right
    operational meaning for storage. The categories below are
    derived from real migration retros (performance tier mismatches
    are by far the most common surprise; replication loss is the
    most catastrophic).
    """

    performance_tier_mismatch = "performance_tier_mismatch"
    capacity_concern = "capacity_concern"
    access_mode_mismatch = "access_mode_mismatch"
    replication_loss = "replication_loss"
    snapshot_capability = "snapshot_capability"
    multipath_policy = "multipath_policy"
    positive_confirmation = "positive_confirmation"


class StorageDesignReview(Base):
    __tablename__ = "storage_design_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[StorageReviewStatus] = mapped_column(
        Enum(StorageReviewStatus, name="storage_review_status"),
        default=StorageReviewStatus.draft,
        server_default=StorageReviewStatus.draft.value,
        nullable=False,
    )
    customer_notes: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    proposed_yaml: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    # Executive summary + meta block. Per-finding rows live in
    # StorageFinding so the UI can filter / sort / triage them
    # efficiently.
    analysis_results: Mapped[dict] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    last_analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    findings: Mapped[list["StorageFinding"]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="StorageFinding.id",
    )


class StorageFinding(Base):
    __tablename__ = "storage_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("storage_design_reviews.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    category: Mapped[StorageFindingCategory] = mapped_column(
        Enum(StorageFindingCategory, name="storage_finding_category"), nullable=False
    )
    severity: Mapped[FindingSeverity] = mapped_column(
        Enum(FindingSeverity, name="storage_finding_severity"), nullable=False
    )
    confidence: Mapped[FindingConfidence] = mapped_column(
        Enum(FindingConfidence, name="storage_finding_confidence"),
        default=FindingConfidence.medium,
        server_default=FindingConfidence.medium.value,
        nullable=False,
    )
    triage: Mapped[FindingTriage] = mapped_column(
        Enum(FindingTriage, name="storage_finding_triage"),
        default=FindingTriage.open,
        server_default=FindingTriage.open.value,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    source_evidence: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    proposed_evidence: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    recommendation: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    review: Mapped[StorageDesignReview] = relationship(back_populates="findings")
