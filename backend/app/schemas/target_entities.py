"""Pydantic schemas for operator-defined TargetNetwork /
TargetStorageClass rows. The mapping-editor dropdowns are populated
from these, so the schemas mirror the model shapes one-for-one.

Enum fields on the *Read* schemas are typed as the enum class itself
(not ``Literal[...]``). Pydantic v2's response-validation pass receives
the ORM's enum instance from ``getattr`` and refuses to coerce it
through a Literal — that was the source of HTTP 500s on the editor
mount. ``use_enum_values=True`` on the read schema then serializes
the enum's ``.value`` for the JSON response."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.target_network import TargetNetworkType
from app.models.target_storage_class import StorageAccessMode


# ---------------------------------------------------------------------------
# TargetNetwork
# ---------------------------------------------------------------------------
class TargetNetworkBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    network_type: TargetNetworkType
    namespace: str | None = Field(default=None, max_length=128)
    is_default: bool = False
    notes: str | None = Field(default=None, max_length=4096)


class TargetNetworkCreate(TargetNetworkBase):
    pass


class TargetNetworkUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    network_type: TargetNetworkType | None = None
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
    access_mode: StorageAccessMode = StorageAccessMode.rwo
    is_default: bool = False
    notes: str | None = Field(default=None, max_length=4096)


class TargetStorageClassCreate(TargetStorageClassBase):
    pass


class TargetStorageClassUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    access_mode: StorageAccessMode | None = None
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
