"""Network design review tables.

A ``NetworkDesignReview`` carries everything VirtValidate needs to run a
gap analysis between the source VMware environment and a proposed
OpenShift Virtualization network design:

  - ``customer_notes`` — operator-provided plain-English context
  - ``proposed_yaml`` — Forklift NetworkMap / Multus NAD / CUDN manifests
  - ``analysis_results`` — the LLM's executive summary + provenance
  - findings live in their own ``NetworkFinding`` table for fast filter
    + per-row triage

Status transitions: draft → analyzing → completed (or back to draft on
analyze failure). Persistent so polling survives appliance restarts; we
don't need a separate task store like the capture flow.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType


class NetworkReviewStatus(str, enum.Enum):
    draft = "draft"
    analyzing = "analyzing"
    completed = "completed"
    failed = "failed"


class FindingCategory(str, enum.Enum):
    coverage_gap = "coverage_gap"
    config_mismatch = "config_mismatch"
    missing_resource = "missing_resource"
    positive_confirmation = "positive_confirmation"


class FindingSeverity(str, enum.Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"
    info = "info"


class FindingConfidence(str, enum.Enum):
    high = "high"
    medium = "medium"
    low = "low"


class FindingTriage(str, enum.Enum):
    """Operator triage state — set after the LLM returned a finding.

    ``open`` is the default; ``accepted`` means the operator agrees and
    will act on it; ``dismissed`` means the operator decided it doesn't
    apply to their environment (false positive, already addressed, etc).
    """

    open = "open"
    accepted = "accepted"
    dismissed = "dismissed"


class NetworkDesignReview(Base):
    __tablename__ = "network_design_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[NetworkReviewStatus] = mapped_column(
        Enum(NetworkReviewStatus, name="network_review_status"),
        default=NetworkReviewStatus.draft,
        server_default=NetworkReviewStatus.draft.value,
        nullable=False,
    )
    customer_notes: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    proposed_yaml: Mapped[str] = mapped_column(Text, default="", server_default="", nullable=False)
    # The LLM's executive summary + meta block. Per-finding rows live in
    # NetworkFinding so the UI can filter/sort/triage them efficiently.
    analysis_results: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
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

    findings: Mapped[list["NetworkFinding"]] = relationship(
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="NetworkFinding.id",
    )


class NetworkFinding(Base):
    __tablename__ = "network_findings"

    id: Mapped[int] = mapped_column(primary_key=True)
    review_id: Mapped[int] = mapped_column(
        ForeignKey("network_design_reviews.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    category: Mapped[FindingCategory] = mapped_column(
        Enum(FindingCategory, name="network_finding_category"), nullable=False
    )
    severity: Mapped[FindingSeverity] = mapped_column(
        Enum(FindingSeverity, name="network_finding_severity"), nullable=False
    )
    confidence: Mapped[FindingConfidence] = mapped_column(
        Enum(FindingConfidence, name="network_finding_confidence"),
        default=FindingConfidence.medium,
        server_default=FindingConfidence.medium.value,
        nullable=False,
    )
    triage: Mapped[FindingTriage] = mapped_column(
        Enum(FindingTriage, name="network_finding_triage"),
        default=FindingTriage.open,
        server_default=FindingTriage.open.value,
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_evidence: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    proposed_evidence: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    recommendation: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    review: Mapped[NetworkDesignReview] = relationship(back_populates="findings")
