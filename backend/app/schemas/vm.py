from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.vm import VMStatus


class VMBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source_hostname: str = Field(min_length=1, max_length=255)
    target_hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    os_family: str | None = Field(default=None, max_length=32)
    role: str | None = Field(default=None, max_length=64)
    notes: str | None = Field(default=None, max_length=1024)


class VMCreate(VMBase):
    pass


class VMUpdate(BaseModel):
    source_hostname: str | None = Field(default=None, max_length=255)
    target_hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    os_family: str | None = Field(default=None, max_length=32)
    role: str | None = Field(default=None, max_length=64)
    status: VMStatus | None = None
    notes: str | None = Field(default=None, max_length=1024)


class VMRead(VMBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: VMStatus
    created_at: datetime
    updated_at: datetime


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
