"""OCP target cluster registry + resource mapping.

A migration runs from one or more :class:`VCenterSource` instances
(VMware) to one or more :class:`OCPTarget` clusters (KubeVirt). Plans
have to know:

  - which OCP cluster they're landing on (target),
  - what resources are *actually* present on that cluster (the
    discovered StorageClasses / NADs / namespaces — not placeholders),
  - how each source vSphere resource maps to a target cluster
    resource (the :class:`ResourceMapping`).

Without these, generated MTV YAML references made-up resource names
that won't apply on the real cluster. That was a real failure mode
in v0.1.x.
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType
from app.models.vcenter import ClassificationLevel


class OCPAuthType(str, enum.Enum):
    """How VirtValidate authenticates to the target cluster.

    ``token`` — operator-supplied OAuth token (most common for
    ad-hoc connections).

    ``service_account`` — in-cluster SA token mounted at the standard
    path. Only available when VirtValidate runs as a pod in the same
    cluster.

    ``kubeconfig`` — multi-step path where the operator pastes a
    full kubeconfig. Useful for clusters with custom certs but
    requires more credential handling.
    """

    token = "token"
    service_account = "service_account"
    kubeconfig = "kubeconfig"


class OCPTargetStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"
    error = "error"


class OCPTarget(Base):
    """A registered OpenShift Virtualization target cluster.

    Discovery results are cached on this row so the mapping editor
    doesn't have to re-query on every render. Refresh is explicit
    via the ``POST /discover`` endpoint.
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
        default=OCPTargetStatus.inactive,
        server_default=OCPTargetStatus.inactive.value,
        nullable=False,
    )

    # ---- Auth ----
    # Bearer token / kubeconfig blob lives in a separate Secret-store
    # row (or k8s Secret in production). The DB only carries the
    # reference name so a SQL dump never includes credentials.
    auth_type: Mapped[OCPAuthType] = mapped_column(
        Enum(OCPAuthType, name="ocp_target_auth_type"),
        default=OCPAuthType.token,
        server_default=OCPAuthType.token.value,
        nullable=False,
    )
    auth_credential_secret_ref: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    # When verify_ssl is False, discovery bypasses cert verification —
    # only set this for dev clusters with self-signed certs. Federal
    # deployments should keep this on.
    verify_ssl: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )

    # ---- Discovery cache ----
    # Each blob is set at discovery time. NULL means "not discovered yet"
    # — the UI surfaces this as "Run discovery to populate."
    storage_classes: Mapped[list[dict] | None] = mapped_column(
        JSONType, nullable=True
    )
    network_attachments: Mapped[list[dict] | None] = mapped_column(
        JSONType, nullable=True
    )
    namespaces: Mapped[list[dict] | None] = mapped_column(
        JSONType, nullable=True
    )
    cluster_capacity: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    mtv_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    ocp_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    kubernetes_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

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

    # Single-active per (vcenter, target) pair. Constraint enforced at
    # the application layer (not a DB partial unique — Postgres only,
    # awkward in batch migrations). The is_active flag is consulted by
    # plan generation when no mapping_id is explicitly supplied.
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    vcenter_source = relationship(
        "VCenterSource", foreign_keys=[vcenter_source_id]
    )
    ocp_target = relationship("OCPTarget", foreign_keys=[ocp_target_id])

    __table_args__ = (
        # Friendly-name uniqueness within a (source, target) pair so
        # operators can have multiple mappings for the same pair (e.g.
        # "Q3 cutover" and "Q4 cutover") without name collisions.
        UniqueConstraint(
            "vcenter_source_id",
            "ocp_target_id",
            "name",
            name="uq_mapping_source_target_name",
        ),
        Index("ix_mappings_source_target", "vcenter_source_id", "ocp_target_id"),
        Index("ix_mappings_status", "status"),
    )
