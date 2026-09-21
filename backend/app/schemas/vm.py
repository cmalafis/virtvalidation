from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.limits import MAX_VMS_PER_BULK_CREATE, MAX_VMS_PER_BULK_DELETE
from app.models.vm import VMLifecycleState, VMStatus


class VMBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_hostname: str = Field(min_length=1, max_length=255)
    target_hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    os_family: str | None = Field(default=None, max_length=32)
    role: str | None = Field(default=None, max_length=64)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    current_platform: str | None = Field(default=None, max_length=64)
    environment: str | None = Field(default=None, max_length=64)
    owner: str | None = Field(default=None, max_length=128)
    notes: str | None = Field(default=None, max_length=1024)
    vsphere_networks: list[str] = Field(default_factory=list)
    vsphere_datastores: list[str] = Field(default_factory=list)
    # Per-VM target overrides. See VM model docstring; NULL means
    # "infer from the matching ResourceMapping" per
    # app.core.target_resolution.
    target_cluster_id_override: int | None = Field(default=None)
    target_namespace_override: str | None = Field(default=None, max_length=253)
    # Multi-vCenter scope. NULL when the operator hasn't tagged the VM
    # to a vCenter (legacy enrollments or manual single-VM adds).
    source_vcenter_id: int | None = Field(default=None)
    # Optional pre-categorization hint — if set at enrollment time,
    # Level 1 will use it as a seed instead of inferring from name patterns.
    application_hint: str | None = Field(default=None, max_length=128)
    # vSphere placement metadata used by the environment detection
    # cascade. NULL when the operator created the VM manually or the
    # RVTools export didn't include the column. All three are
    # advisory — the planner reads them when present but doesn't
    # require them.
    vsphere_cluster: str | None = Field(default=None, max_length=255)
    vsphere_folder: str | None = Field(default=None, max_length=512)
    custom_attributes: dict = Field(default_factory=dict)


class VMCreate(VMBase):
    pass


class VMUpdate(BaseModel):
    source_hostname: str | None = Field(default=None, max_length=255)
    target_hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    os_family: str | None = Field(default=None, max_length=32)
    role: str | None = Field(default=None, max_length=64)
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    current_platform: str | None = Field(default=None, max_length=64)
    environment: str | None = Field(default=None, max_length=64)
    owner: str | None = Field(default=None, max_length=128)
    status: VMStatus | None = None
    # Operator-driven plan-membership transitions. Only ``migrated →
    # rolled_back → available`` is accepted by the route; the route
    # rejects any other delta with 422 so the UI cannot bypass the
    # server-enforced state machine.
    lifecycle_state: VMLifecycleState | None = None
    notes: str | None = Field(default=None, max_length=1024)
    vsphere_networks: list[str] | None = None
    vsphere_datastores: list[str] | None = None
    target_cluster_id_override: int | None = Field(default=None)
    target_namespace_override: str | None = Field(default=None, max_length=253)
    source_vcenter_id: int | None = Field(default=None)
    application_hint: str | None = Field(default=None, max_length=128)


class ResolvedNetworkRead(BaseModel):
    """One source-network resolution row surfaced to the inventory UI."""

    source: str
    target_network_id: int | None = None
    target_network_name: str | None = None
    target_network_namespace: str | None = None
    target_network_type: str | None = None


class ResolvedStorageRead(BaseModel):
    source: str
    target_storage_class_name: str | None = None
    access_mode: str | None = None


class VMRead(VMBase):
    # use_enum_values=True so Pydantic serializes both ``status`` and
    # ``lifecycle_state`` as their string values rather than enum
    # repr-strings — the frontend filters on plain values, and the
    # facets endpoint returns the same string form.
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    status: VMStatus
    lifecycle_state: VMLifecycleState
    lifecycle_state_changed_at: datetime
    # vSphere identity + sizing, populated by the RVTools importer.
    # ``hardware_facts`` is deliberately not on the list payload — it is
    # a per-VM document (disks, snapshots, NICs); fetch it per VM.
    moref: str | None = None
    vm_uuid: str | None = None
    power_state: str | None = None
    esxi_host: str | None = None
    vsphere_datacenter: str | None = None
    guest_os_full: str | None = None
    num_cpus: int | None = None
    memory_mb: int | None = None
    disk_count: int | None = None
    nic_count: int | None = None
    provisioned_mb: int | None = None
    missing_from_last_upload: bool = False
    created_at: datetime
    updated_at: datetime

    # Resolved-target fields populated by :func:`list_vms`,
    # :func:`get_vm`, and the bulk override endpoints. Defaults keep
    # the schema compatible with detached VM rows (test fixtures,
    # background jobs) that haven't gone through the resolver.
    resolved_target_cluster_id: int | None = None
    resolved_target_cluster_name: str | None = None
    resolved_target_namespace: str | None = None
    resolved_networks: list[ResolvedNetworkRead] = Field(default_factory=list)
    resolved_storage: list[ResolvedStorageRead] = Field(default_factory=list)
    resolution_is_complete: bool = False
    resolution_reasons: list[str] = Field(default_factory=list)
    resolution_mapping_id: int | None = None


class VMListResponse(BaseModel):
    """Paginated wrapper for the inventory listing.

    The frontend's data table needs the total count separately from
    the rendered page so it can show "X to Y of Z" and compute the
    page count without a second round-trip. ``skip`` + ``limit`` echo
    the resolved values so the caller can detect when its requested
    page was clipped by the backend's ceiling.
    """

    items: list[VMRead]
    total: int
    skip: int
    limit: int


class VMFacetsResponse(BaseModel):
    """Per-dimension counts driven by the same filter set as list_vms.

    Each value is a ``{value: count}`` map. Keys are stringified so
    JSON output stays uniform across enum-backed and free-form
    columns (status is an enum, environment is free-form text).
    """

    status: dict[str, int]
    lifecycle_state: dict[str, int]
    environment: dict[str, int]
    os_family: dict[str, int]
    application_hint: dict[str, int]
    vcenter_source_id: dict[str, int]
    classification_level: dict[str, int]
    vsphere_cluster: dict[str, int] = Field(default_factory=dict)
    power_state: dict[str, int] = Field(default_factory=dict)
    total: int


class VMStats(BaseModel):
    """Cheap aggregate counters for dashboard headers.

    Mirrors what the inventory list filter dropdowns would derive
    from facets, but without echoing per-value counts. Cheap because
    the queries are bare ``COUNT(*) GROUP BY status`` — no row reads.
    """

    total: int
    by_status: dict[str, int]
    missing_from_last_upload: int


class DeleteAllVMsResult(BaseModel):
    deleted_count: int


class EnvironmentSetRequest(BaseModel):
    """Body for PATCH /api/vms/{id}/environment.

    Operator override — the API validates the value against the
    canonical Environment enum via ``normalize()`` and rejects
    anything that doesn't map. ``rationale`` is recorded in the
    audit log for federal compliance review.
    """

    environment: str = Field(min_length=1, max_length=64)
    rationale: str | None = Field(default=None, max_length=512)


class BulkEnvironmentSetRequest(BaseModel):
    """Body for POST /api/vms/bulk-set-environment.

    Atomic — either every vm_id updates or none do. Use sparingly:
    the planner partitions by environment, so bulk-changing the
    environment of in-flight VMs across an existing plan will
    invalidate the plan's wave structure.
    """

    vm_ids: list[int] = Field(min_length=1, max_length=MAX_VMS_PER_BULK_CREATE)
    environment: str = Field(min_length=1, max_length=64)
    rationale: str | None = Field(default=None, max_length=512)


class BulkEnvironmentSetResult(BaseModel):
    updated: int
    not_found: list[int]


class RedetectEnvironmentRequest(BaseModel):
    """Body for POST /api/vms/redetect-environment.

    ``force=true`` overwrites VMs marked ``user_set`` — only use
    after the operator confirms they want to discard manual
    labels (e.g. a major fleet re-import). ``dry_run=true``
    returns the projected changes without persisting them.
    """

    vcenter_source_id: int | None = Field(default=None)
    force: bool = False
    dry_run: bool = False


class RedetectEnvironmentResult(BaseModel):
    scanned: int
    updated: int
    skipped_user_set: int
    still_unknown: int
    summary_by_environment: dict[str, int]
    still_unknown_samples: list[str]


class BulkVMCreate(BaseModel):
    # See app.core.limits.MAX_VMS_PER_BULK_CREATE — sized for federal
    # customer scale (10K VMs in one RVTools import). Override via
    # MAX_VMS_PER_BULK_CREATE env var when running smaller deployments.
    vms: list[VMCreate] = Field(min_length=1, max_length=MAX_VMS_PER_BULK_CREATE)


class BulkVMSkipped(BaseModel):
    name: str
    reason: str


class BulkVMResult(BaseModel):
    total: int
    created: list[VMRead]
    skipped: list[BulkVMSkipped]


class BulkVMDelete(BaseModel):
    # Same cap as BulkVMCreate — see app.core.limits.MAX_VMS_PER_BULK_DELETE.
    vm_ids: list[int] = Field(min_length=1, max_length=MAX_VMS_PER_BULK_DELETE)


class BulkVMDeleteResult(BaseModel):
    requested: int
    deleted: list[int]
    not_found: list[int]


# ---------- bulk target override endpoints ----------
class BulkSetTargetClusterRequest(BaseModel):
    """Body for POST /api/vms/bulk-set-target-cluster.

    ``target_cluster_id_override`` accepts an int (apply override) or
    ``None`` (clear override). The dedicated bulk-clear endpoint is a
    thin alias for callers that want intent-named routes.
    """

    vm_ids: list[int] = Field(min_length=1, max_length=MAX_VMS_PER_BULK_CREATE)
    target_cluster_id_override: int | None = Field(default=None)


class BulkSetTargetNamespaceRequest(BaseModel):
    vm_ids: list[int] = Field(min_length=1, max_length=MAX_VMS_PER_BULK_CREATE)
    target_namespace_override: str | None = Field(default=None, max_length=253)


class BulkClearTargetRequest(BaseModel):
    vm_ids: list[int] = Field(min_length=1, max_length=MAX_VMS_PER_BULK_CREATE)


class BulkTargetOverrideResult(BaseModel):
    updated: int
    not_found: list[int]


# ---------- on-demand capture ----------


class CaptureTaskRead(BaseModel):
    task_id: str
    vm_id: int
    status: Literal["running", "completed", "failed"]
    started_at: datetime
    completed_at: datetime | None = None
    snapshot_id: int | None = None
    error: str | None = None


class BulkCaptureSpawn(BaseModel):
    vm_id: int
    task_id: str


class BulkCaptureResult(BaseModel):
    spawned: list[BulkCaptureSpawn]
    skipped: list[dict]  # [{"vm_id": int, "reason": str}, ...]


class SnapshotCreate(BaseModel):
    ssh_user: str = Field(min_length=1, max_length=64)
    raw_data: dict
    checksum: str | None = Field(default=None, max_length=64)


class SnapshotRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vm_id: int
    snapshot_number: int
    ssh_user: str
    raw_data: dict
    checksum: str | None
    collected_at: datetime


class BaselineProfile(BaseModel):
    vm_id: int
    snapshot_count: int
    first_collected_at: datetime | None
    last_collected_at: datetime | None
    latest_meta: dict
    services: list[str]
    open_ports: list[dict]
    stable_mounts: list[dict]
    dns_servers: list[str]
    interfaces: dict
