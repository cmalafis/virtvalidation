"""Operator-declared target namespaces on an OCP cluster.

Each row represents one Kubernetes namespace that the operator declares
is a valid migration target on a registered cluster. The PlanWizard's
typeahead and the inventory's Target Namespace cell pull from this
catalog. As with :mod:`app.models.target_network` and
:mod:`app.models.target_storage_class`, live cluster discovery is
intentionally not the source of truth — air-gapped federal deployments
declare what's available rather than letting the appliance probe the
cluster API.

MTV auto-creates target namespaces at plan-apply time if they don't
yet exist on the cluster, so the catalog is operator intent, not
cluster ground truth.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class OCPTargetNamespace(Base):
    __tablename__ = "ocp_target_namespaces"

    id: Mapped[int] = mapped_column(primary_key=True)
    ocp_target_id: Mapped[int] = mapped_column(
        ForeignKey("ocp_targets.id", ondelete="CASCADE"), nullable=False
    )
    # 253 chars matches the RFC1123 cap for a Kubernetes namespace
    # resource name. Individual labels within the name are bounded at
    # 63 chars by k8s but the full name can use the whole 253.
    name: Mapped[str] = mapped_column(String(253), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
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
        UniqueConstraint("ocp_target_id", "name", name="uq_namespace_per_cluster"),
        Index("ix_namespace_ocp_target", "ocp_target_id"),
    )
