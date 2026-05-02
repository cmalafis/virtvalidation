import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType


class VMStatus(str, enum.Enum):
    discovered = "discovered"
    baseline_captured = "baseline_captured"
    migrated = "migrated"
    validated = "validated"
    failed = "failed"


class VM(Base):
    __tablename__ = "vms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_hostname: Mapped[str] = mapped_column(String(255))
    target_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    os_family: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ssh_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ssh_port: Mapped[int] = mapped_column(Integer, default=22, server_default="22", nullable=False)
    # Free-form inventory metadata captured at enrollment time. Not used by
    # the SSH collector itself; surfaced in the inventory UI and CSV export
    # so federal customers can reconcile rows against their CMDB.
    current_platform: Mapped[str | None] = mapped_column(String(64), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[VMStatus] = mapped_column(
        Enum(VMStatus, name="vm_status"), default=VMStatus.discovered, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # MTV migration mapping fields. Source side describes what the VM is wired
    # to in vSphere; target side describes what it should land on in OCP-Virt.
    # Lists are stored as JSON arrays so a VM with multiple NICs/disks can map
    # cleanly through Forklift NetworkMap/StorageMap.
    vsphere_networks: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    vsphere_datastores: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    target_namespace: Mapped[str | None] = mapped_column(String(253), nullable=True)
    target_storage_class: Mapped[str | None] = mapped_column(String(253), nullable=True)
    target_network_attachment: Mapped[str | None] = mapped_column(String(253), nullable=True)

    # Multi-vCenter boundary. NULL is allowed for back-compat — VMs
    # enrolled before vCenter source registration was added stay
    # ungrouped until an operator backfills. SET NULL on delete so
    # removing a vCenter source doesn't cascade-delete its VMs (federal
    # operators want a confirmation step, not a silent purge).
    source_vcenter_id: Mapped[int | None] = mapped_column(
        ForeignKey("vcenter_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Free-form application hint used by Level 1 categorization to seed
    # group inference. The LLM may overwrite this with what it deduces
    # from naming patterns + role + custom attributes. Operators can
    # also fill it manually when they know the application boundary
    # ahead of time (e.g., "epic-emr-prod", "athena-billing").
    application_hint: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    # Hot query paths for scale-aware planning. These match the access
    # patterns the scope-selection wizard and Level 1 categorizer drive:
    #   - "VMs in vCenter X with status Y" — inventory pages
    #   - "VMs in target cluster N" — campaign/wave assignment views
    #   - "VMs by app + env" — Level 1 grouping aggregator
    __table_args__ = (
        Index("ix_vms_source_vcenter_status", "source_vcenter_id", "status"),
        Index("ix_vms_target_namespace", "target_namespace"),
        Index("ix_vms_app_env", "application_hint", "environment"),
    )

    snapshots: Mapped[list["BaselineSnapshot"]] = relationship(
        back_populates="vm",
        cascade="all, delete-orphan",
        order_by="BaselineSnapshot.collected_at.desc()",
    )
    # Validations cascade-delete with the VM at both the ORM and DB level.
    # The DB-side ondelete="CASCADE" lives on ValidationResult.vm_id; the
    # ORM cascade here ensures `db.delete(vm)` cleans rows up in the same
    # transaction (also matters under SQLite tests, where FK PRAGMAs vary).
    validations: Mapped[list["ValidationResult"]] = relationship(  # noqa: F821
        "ValidationResult",
        back_populates="vm",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BaselineSnapshot(Base):
    __tablename__ = "baseline_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    vm_id: Mapped[int] = mapped_column(ForeignKey("vms.id", ondelete="CASCADE"), index=True)
    snapshot_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ssh_user: Mapped[str] = mapped_column(String(64))
    raw_data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    collected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    vm: Mapped[VM] = relationship(back_populates="snapshots")

    __table_args__ = (
        Index("ix_snapshots_vm_collected_at", "vm_id", "collected_at"),
        Index(
            "ix_snapshots_vm_snapshot_number",
            "vm_id",
            "snapshot_number",
            unique=True,
        ),
    )
