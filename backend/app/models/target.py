"""OCP target cluster registry + resource mapping.

A migration runs from one or more :class:`VCenterSource` instances
(VMware) to one or more :class:`OCPTarget` clusters (KubeVirt). Plans
have to know:

  - which OCP cluster they're landing on (target),
  - which target namespaces / NADs / StorageClasses the operator
    declares are available on that cluster (operator-declared
    catalogs in ``ocp_target_namespaces`` / ``target_networks`` /
    ``target_storage_classes`` — VirtValidate does not authenticate
    to clusters, the operator declares ground truth),
  - how each source vSphere resource maps to a target cluster
    resource (the :class:`ResourceMapping`, unique per
    ``(vcenter_source_id, ocp_target_id)``).

Without these, generated MTV YAML references made-up resource names
that won't apply on the real cluster. That was a real failure mode
in v0.1.x.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType
from app.models.vcenter import ClassificationLevel


class OCPTargetStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    error = "error"


class OCPTarget(Base):
    """A registered OpenShift Virtualization target cluster.

    Metadata only — VirtValidate does not authenticate to clusters.
    ``api_endpoint`` is informational (written into emitted MTV
    Provider YAML, never opened by the appliance). Available
    resources are declared via the per-cluster catalogs
    (``ocp_target_namespaces``, ``target_networks``,
    ``target_storage_classes``).
    """

    __tablename__ = "ocp_targets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    api_endpoint: Mapped[str] = mapped_column(String(512), nullable=False)
    region: Mapped[str | None] = mapped_column(String(64), nullable=True)
    site: Mapped[str | None] = mapped_column(String(64), nullable=True)
    classification_level: Mapped[ClassificationLevel] = mapped_column(
        Enum(ClassificationLevel, name="ocp_target_classification_level"),
        default=ClassificationLevel.unclassified,
        server_default=ClassificationLevel.unclassified.value,
        nullable=False,
    )
    status: Mapped[OCPTargetStatus] = mapped_column(
        Enum(OCPTargetStatus, name="ocp_target_status"),
        default=OCPTargetStatus.active,
        server_default=OCPTargetStatus.active.value,
        nullable=False,
    )

    mtv_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    ocp_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kubernetes_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
        Index("ix_ocp_targets_status", "status"),
        Index("ix_ocp_targets_classification", "classification_level"),
    )


class ResourceMappingStatus(str, enum.Enum):
    """Lifecycle of a mapping. ``incomplete`` means at least one
    source resource still has no target. ``needs_review`` means
    drift detection found target resources that have moved /
    disappeared since the mapping was last saved."""

    complete = "complete"
    incomplete = "incomplete"
    needs_review = "needs_review"


class ResourceMapping(Base):
    """Concrete network/storage/namespace mapping between a vCenter
    source and an OCP target. The plan generator pulls from here when
    rendering MTV YAML so the YAML references real cluster resources
    (not placeholders)."""

    __tablename__ = "resource_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    vcenter_source_id: Mapped[int] = mapped_column(
        ForeignKey("vcenter_sources.id", ondelete="CASCADE"),
        nullable=False,
    )
    ocp_target_id: Mapped[int] = mapped_column(
        ForeignKey("ocp_targets.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[ResourceMappingStatus] = mapped_column(
        Enum(ResourceMappingStatus, name="resource_mapping_status"),
        default=ResourceMappingStatus.incomplete,
        server_default=ResourceMappingStatus.incomplete.value,
        nullable=False,
    )

    # ---- Mapping payloads ----
    # Each is a list of objects. Schemas are documented in
    # ``app.schemas.target.NetworkMappingItem`` / ``StorageMappingItem``
    # / ``NamespaceMappingItem``. JSONB so we can grow new fields
    # without an Alembic migration per addition.
    network_mappings: Mapped[list[dict]] = mapped_column(
        JSONType, nullable=False, default=list, server_default="[]"
    )
    storage_mappings: Mapped[list[dict]] = mapped_column(
        JSONType, nullable=False, default=list, server_default="[]"
    )
    namespace_mappings: Mapped[list[dict]] = mapped_column(
        JSONType, nullable=False, default=list, server_default="[]"
    )

    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    vcenter_source = relationship("VCenterSource", foreign_keys=[vcenter_source_id])
    ocp_target = relationship("OCPTarget", foreign_keys=[ocp_target_id])

    __table_args__ = (
        # Exactly one mapping per (vcenter, target) pair. MTV's
        # NetworkMap / StorageMap CRs are themselves scoped to a
        # single (source provider, destination provider) pair, so a
        # single mapping is the natural granularity. Operators
        # disambiguate by cluster, not by names like "Q3 cutover".
        UniqueConstraint(
            "vcenter_source_id",
            "ocp_target_id",
            name="uq_mapping_per_pair",
        ),
        Index("ix_mappings_source_target", "vcenter_source_id", "ocp_target_id"),
        Index("ix_mappings_status", "status"),
    )
