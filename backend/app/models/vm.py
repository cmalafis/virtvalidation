import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType


class VMStatus(str, enum.Enum):
    discovered = "discovered"
    baseline_captured = "baseline_captured"
    migrated = "migrated"
    validated = "validated"
    failed = "failed"


class VMLifecycleState(str, enum.Enum):
    """Plan-membership lifecycle, orthogonal to ``VMStatus``.

    ``VMStatus`` answers "is this VM operational?" (baseline captured,
    validated, etc.). ``VMLifecycleState`` answers "is this VM available
    to be assigned to a new plan?" — driven by plan membership, not
    technical health.

    Transitions are server-enforced:

      available  → planned          on POST /api/plans
      planned    → migrated         on POST /api/plans/{id}/mark-succeeded
      planned    → available        on DELETE /api/plans/{id} or plan→failed
      migrated   → rolled_back      on PATCH /api/vms/{id}
      rolled_back → available       on PATCH /api/vms/{id}
      (any)      → unmanageable     informational; not produced by hooks

    The selector UI shows only ``available`` VMs by default; toggling
    other states is allowed for auditing but those rows are not
    selectable.
    """

    available = "available"
    planned = "planned"
    migrated = "migrated"
    rolled_back = "rolled_back"
    unmanageable = "unmanageable"


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
    # Plan-membership lifecycle. See VMLifecycleState docstring; the
    # selector hides anything not ``available`` by default.
    lifecycle_state: Mapped[VMLifecycleState] = mapped_column(
        Enum(VMLifecycleState, name="vm_lifecycle_state"),
        default=VMLifecycleState.available,
        server_default=VMLifecycleState.available.value,
        nullable=False,
        index=True,
    )
    lifecycle_state_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
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

    # vSphere placement metadata. The RVTools export carries these
    # columns; the preclassifier + environment detector read them to
    # support tier-2/tier-3 detection (folder pattern, cluster name
    # pattern) and richer partition keys. NULL when the importer
    # didn't see the column or the operator created the VM manually.
    vsphere_cluster: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vsphere_folder: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Custom Attributes from RVTools — a dict of operator-supplied
    # labels (Environment, App, Tier, Owner). Tier-2 of the env
    # detector reads ``custom_attributes["Environment"]`` first. Free-
    # form so any key the customer ships is preserved verbatim for
    # audit; the planner only consumes the keys it knows about.
    custom_attributes: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # Provenance of the ``environment`` field. ``unset`` = never
    # touched; ``auto_detected`` = the importer or redetect job
    # populated it; ``user_set`` = an operator explicitly chose it.
    # Redetect-by-default skips user_set so operator overrides
    # survive bulk re-runs.
    environment_source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="unset",
        server_default="unset",
    )

    # RVTools delta-import lifecycle. ``missing_from_last_upload`` is
    # set on VMs that were in the prior upload but absent from the
    # latest one — operators decide whether to decommission or keep.
    # ``last_seen_in_upload_at`` records when the row was last
    # observed in any RVTools import; together these support a "stale
    # inventory" filter without auto-deleting rows.
    missing_from_last_upload: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="0",
        nullable=False,
    )
    last_seen_in_upload_at: Mapped[datetime | None] = mapped_column(
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
