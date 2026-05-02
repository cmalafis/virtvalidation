"""vCenter source registry — separate model from VM so a single
deployment can manage VMs from multiple distinct vCenter environments.

Federal customers commonly run several vCenters across regions /
classification levels (unclass / CUI / SECRET) and need the migration
appliance to keep them isolated. This model is the boundary that
isolation rides on.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class ClassificationLevel(str, enum.Enum):
    """DoD-style data classification. Defaults to ``unclassified`` so
    deployments without a federal compliance posture don't have to
    populate the field. The Settings UI surfaces this as a read-only
    badge per vCenter so operators can see the boundary at a glance."""

    unclassified = "unclassified"
    cui = "cui"
    secret = "secret"
    top_secret = "top_secret"


class VCenterStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    archived = "archived"


class VCenterSource(Base):
    """Logical handle for a vCenter that VMs belong to.

    The relationship is one-to-many — one vCenter, many VMs. Cross-
    vCenter VMs share no name uniqueness constraint here (the VM table
    still enforces global name uniqueness, but customers commonly
    rename VMs at enrollment to ``<vcenter-shortname>-<original>`` to
    avoid collisions).
    """

    __tablename__ = "vcenter_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification_level: Mapped[ClassificationLevel] = mapped_column(
        Enum(ClassificationLevel, name="classification_level"),
        default=ClassificationLevel.unclassified,
        server_default=ClassificationLevel.unclassified.value,
        nullable=False,
    )
    status: Mapped[VCenterStatus] = mapped_column(
        Enum(VCenterStatus, name="vcenter_status"),
        default=VCenterStatus.active,
        server_default=VCenterStatus.active.value,
        nullable=False,
    )
    # Default mappings applied to VMs imported under this vCenter when the
    # operator hasn't set per-VM target_namespace. Keeps the bulk-import
    # path from forcing the operator to set every field manually.
    default_target_namespace: Mapped[str | None] = mapped_column(
        String(253), nullable=True
    )
    default_target_storage_class: Mapped[str | None] = mapped_column(
        String(253), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_vcenter_status", "status"),
        Index("ix_vcenter_classification", "classification_level"),
    )
