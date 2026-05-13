"""Pydantic schemas for operator-defined TargetNetwork /
TargetStorageClass rows. The mapping-editor dropdowns are populated
from these, so the schemas mirror the model shapes one-for-one."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# TargetNetwork
# ---------------------------------------------------------------------------
class TargetNetworkBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    network_type: Literal["nad", "cudn", "udn", "pod"]
    namespace: str | None = Field(default=None, max_length=128)
    is_default: bool = False
    notes: str | None = Field(default=None, max_length=4096)


class TargetNetworkCreate(TargetNetworkBase):
    pass


class TargetNetworkUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    network_type: Literal["nad", "cudn", "udn", "pod"] | None = None
    namespace: str | None = Field(default=None, max_length=128)
    is_default: bool | None = None
    notes: str | None = Field(default=None, max_length=4096)


class TargetNetworkRead(TargetNetworkBase):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    ocp_target_id: int
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# TargetStorageClass
# ---------------------------------------------------------------------------
class TargetStorageClassBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    access_mode: Literal["ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany"] = "ReadWriteOnce"
    is_default: bool = False
    notes: str | None = Field(default=None, max_length=4096)


class TargetStorageClassCreate(TargetStorageClassBase):
    pass


class TargetStorageClassUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    access_mode: Literal["ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany"] | None = None
    is_default: bool | None = None
    notes: str | None = Field(default=None, max_length=4096)


class TargetStorageClassRead(TargetStorageClassBase):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    ocp_target_id: int
    created_at: datetime
    updated_at: datetime


class TargetEntityDeleteConflict(BaseModel):
    """409 body shape returned when a TargetNetwork or TargetStorageClass
    is referenced by an active ResourceMapping. The operator must
    unmap before deletion succeeds."""

    detail: str
    referenced_by: list[dict] = Field(default_factory=list)
