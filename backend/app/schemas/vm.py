from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.vm import VMStatus


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
    target_namespace: str | None = Field(default=None, max_length=253)
    target_storage_class: str | None = Field(default=None, max_length=253)
    target_network_attachment: str | None = Field(default=None, max_length=253)


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
    notes: str | None = Field(default=None, max_length=1024)
    vsphere_networks: list[str] | None = None
    vsphere_datastores: list[str] | None = None
    target_namespace: str | None = Field(default=None, max_length=253)
    target_storage_class: str | None = Field(default=None, max_length=253)
    target_network_attachment: str | None = Field(default=None, max_length=253)


class VMRead(VMBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: VMStatus
    created_at: datetime
    updated_at: datetime


class BulkVMCreate(BaseModel):
    vms: list[VMCreate] = Field(min_length=1, max_length=500)


class BulkVMSkipped(BaseModel):
    name: str
    reason: str


class BulkVMResult(BaseModel):
    total: int
    created: list[VMRead]
    skipped: list[BulkVMSkipped]


class BulkVMDelete(BaseModel):
    vm_ids: list[int] = Field(min_length=1, max_length=500)


class BulkVMDeleteResult(BaseModel):
    requested: int
    deleted: list[int]
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
