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
from ipaddress import ip_network
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.target_network import TargetNetworkType
from app.models.target_storage_class import StorageAccessMode

# Value sets live in app.core.platform_features (NETWORK_ROLES, …); the
# Literals below must match them — pinned by tests/test_platform_features.py.
NetworkRole = Literal["primary", "secondary"]
NetworkTopology = Literal["layer2", "layer3", "localnet", "bridge", "sriov", "other"]
IpamMode = Literal["persistent", "disabled", "none"]
Binding = Literal["l2bridge", "passt", "bridge", "sriov"]
Transport = Literal["geneve", "evpn"]
VolumeMode = Literal["Filesystem", "Block"]
CopyOffloadVendor = Literal[
    "flashsystem",
    "vantara",
    "ontap",
    "primera3par",
    "pureFlashArray",
    "powerflex",
    "powermax",
    "powerstore",
    "infinibox",
]

# Optional OVN-Kubernetes attributes. PATCH may set any of them to null
# to clear it (unlike name / type, which PATCH never clears).
NETWORK_ATTRIBUTE_FIELDS = (
    "role",
    "topology",
    "vlan_id",
    "physical_network_name",
    "mtu",
    "subnets",
    "ipam_mode",
    "binding",
    "transport",
)
STORAGE_ATTRIBUTE_FIELDS = (
    "volume_mode",
    "provisioner",
    "copy_offload_vendor",
    "copy_offload_secret",
)


# ---------------------------------------------------------------------------
# TargetNetwork
# ---------------------------------------------------------------------------
class _NetworkAttributes(BaseModel):
    role: NetworkRole | None = None
    topology: NetworkTopology | None = None
    vlan_id: int | None = Field(default=None, ge=0, le=4094)
    physical_network_name: str | None = Field(default=None, max_length=128)
    mtu: int | None = Field(default=None, ge=576, le=9216)
    subnets: list[str] | None = Field(default=None, max_length=8)
    ipam_mode: IpamMode | None = None
    binding: Binding | None = None
    transport: Transport | None = None

    @field_validator("subnets")
    @classmethod
    def _cidrs(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        out = []
        for raw in v:
            try:
                out.append(str(ip_network(raw.strip(), strict=False)))
            except ValueError as e:
                raise ValueError(f"not a CIDR: {raw!r}") from e
        return out


class TargetNetworkBase(_NetworkAttributes):
    name: str = Field(min_length=1, max_length=128)
    network_type: TargetNetworkType
    namespace: str | None = Field(default=None, max_length=128)
    is_default: bool = False
    notes: str | None = Field(default=None, max_length=4096)


class TargetNetworkCreate(TargetNetworkBase):
    pass


class TargetNetworkUpdate(_NetworkAttributes):
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
class _StorageAttributes(BaseModel):
    volume_mode: VolumeMode | None = None
    provisioner: str | None = Field(default=None, max_length=253)
    copy_offload_vendor: CopyOffloadVendor | None = None
    copy_offload_secret: str | None = Field(default=None, max_length=253)


class TargetStorageClassBase(_StorageAttributes):
    name: str = Field(min_length=1, max_length=128)
    access_mode: StorageAccessMode = StorageAccessMode.rwo
    is_default: bool = False
    notes: str | None = Field(default=None, max_length=4096)


class TargetStorageClassCreate(TargetStorageClassBase):
    pass


class TargetStorageClassUpdate(_StorageAttributes):
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


# ---------------------------------------------------------------------------
# Catalog import (pasted ``oc get … -o yaml``)
# ---------------------------------------------------------------------------
class CatalogImportRequest(BaseModel):
    # 5 MiB — app.core.catalog_import.MAX_IMPORT_BYTES
    yaml: str = Field(min_length=1, max_length=5 * 1024 * 1024)
    # Preview by default; the UI always previews before applying.
    dry_run: bool = True


class CatalogImportResponse(BaseModel):
    """Every derived entry with ``action`` create / update / unchanged."""

    dry_run: bool
    applied: bool = False
    networks: list[dict] = Field(default_factory=list)
    storage_classes: list[dict] = Field(default_factory=list)
    namespaces: list[dict] = Field(default_factory=list)
    localnet_bridge_mappings: list[str] | None = None
    warnings: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
