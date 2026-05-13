"""Operator-defined target network entities on an OCP cluster.

Each row represents one NetworkAttachmentDefinition / CUDN / UDN that
the operator declares is available on the target cluster. The mapping
editor dropdowns are populated from these rows, not from live cluster
discovery — the design is "operator tells the appliance what's there"
because air-gapped deployments often can't reach the cluster API.
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
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class TargetNetworkType(str, enum.Enum):
    nad = "nad"
    cudn = "cudn"
    udn = "udn"
    pod = "pod"


class TargetNetwork(Base):
    __tablename__ = "target_networks"

    id: Mapped[int] = mapped_column(primary_key=True)
    ocp_target_id: Mapped[int] = mapped_column(
        ForeignKey("ocp_targets.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    network_type: Mapped[TargetNetworkType] = mapped_column(
        Enum(TargetNetworkType, name="target_network_type"), nullable=False
    )
    namespace: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        UniqueConstraint("ocp_target_id", "name", name="uq_target_network_per_cluster"),
        Index("ix_target_network_ocp_target", "ocp_target_id"),
    )
