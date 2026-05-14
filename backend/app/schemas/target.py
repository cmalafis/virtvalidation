from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.models.target import OCPTargetStatus, ResourceMappingStatus
from app.models.vcenter import ClassificationLevel


def _drop_non_dict_rows(value: Any) -> Any:
    """Pydantic before-validator that strips non-dict elements from a
    list. The mapping JSON columns occasionally contain ``None`` or
    stray strings from legacy versions; we'd rather quietly drop those
    rows than 500 the read with a response-validation error."""
    if isinstance(value, list):
        return [v for v in value if isinstance(v, dict)]
    return value


_DictRowList = Annotated[list[dict], BeforeValidator(_drop_non_dict_rows)]


# ---------------------------------------------------------------------------
# OCPTarget — metadata only; VirtValidate does not authenticate to clusters.
# Available cluster resources are operator-declared via the per-cluster
# catalogs (ocp_target_namespaces, target_networks, target_storage_classes).
# ---------------------------------------------------------------------------
class OCPTargetBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    api_endpoint: str = Field(min_length=1, max_length=512)
    region: str | None = Field(default=None, max_length=64)
    site: str | None = Field(default=None, max_length=64)
    classification_level: ClassificationLevel = ClassificationLevel.unclassified
    mtv_namespace: str | None = Field(default=None, max_length=253)
    ocp_version: str | None = Field(default=None, max_length=32)
    kubernetes_version: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=4096)


class OCPTargetCreate(OCPTargetBase):
    pass


class OCPTargetUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    api_endpoint: str | None = Field(default=None, min_length=1, max_length=512)
    region: str | None = Field(default=None, max_length=64)
    site: str | None = Field(default=None, max_length=64)
    classification_level: ClassificationLevel | None = None
    mtv_namespace: str | None = Field(default=None, max_length=253)
    ocp_version: str | None = Field(default=None, max_length=32)
    kubernetes_version: str | None = Field(default=None, max_length=32)
    notes: str | None = Field(default=None, max_length=4096)


class OCPTargetRead(OCPTargetBase):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    status: OCPTargetStatus
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# ResourceMapping
# ---------------------------------------------------------------------------
class NetworkMappingItem(BaseModel):
    """One source network → target network row."""

    source_network: str = Field(min_length=1, max_length=255)
    target_network_name: str | None = Field(default=None, max_length=255)
    target_network_type: Literal["nad", "cudn", "udn", "pod"] | None = None
    target_namespace: str | None = Field(default=None, max_length=253)
    confidence: Literal["high", "medium", "low"] | None = None
    rationale: str | None = Field(default=None, max_length=1024)


class StorageMappingItem(BaseModel):
    source_datastore: str = Field(min_length=1, max_length=255)
    target_storage_class: str | None = Field(default=None, max_length=253)
    access_mode: Literal["ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany"] | None = None
    confidence: Literal["high", "medium", "low"] | None = None
    rationale: str | None = Field(default=None, max_length=1024)


class NamespaceMappingItem(BaseModel):
    """How VMs land into target namespaces. ``criteria`` is one of
    ``default`` (all VMs) / ``environment`` (env=value) /
    ``application`` (app_hint=value) / ``vcenter_folder``. The
    ``criteria_value`` field carries the matched value. Mapping
    resolution walks the list in order — first match wins."""

    criteria: Literal["default", "environment", "application", "vcenter_folder"] = "default"
    criteria_value: str | None = Field(default=None, max_length=255)
    target_namespace: str = Field(min_length=1, max_length=253)


class NamespaceStrategy(BaseModel):
    """Newer namespace-mapping shape: instead of a list of criteria
    rows, the operator picks a single strategy. The plan generator
    resolves each VM's namespace by dispatching on the strategy.

    Stored as a JSON object on ``ResourceMapping.namespace_mappings``.
    The column happily holds either this shape or the legacy
    list[NamespaceMappingItem] — the resolver dispatches at read time.
    """

    strategy: Literal["single", "per_environment", "per_application"] = "per_environment"
    single_namespace: str | None = Field(default=None, max_length=253)
    per_env_namespaces: dict[str, str] = Field(default_factory=dict)
    per_app_prefix: str = Field(default="app", max_length=64)


class ResourceMappingBase(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    vcenter_source_id: int
    ocp_target_id: int
    network_mappings: list[NetworkMappingItem] = Field(default_factory=list)
    storage_mappings: list[StorageMappingItem] = Field(default_factory=list)
    # The namespace mapping is either a list of criteria rows (legacy)
    # or a strategy dict (new flow). Pydantic resolves the union by
    # shape; the column type stays JSON either way.
    namespace_mappings: list[NamespaceMappingItem] | NamespaceStrategy = Field(default_factory=list)


class ResourceMappingCreate(ResourceMappingBase):
    pass


class ResourceMappingUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    network_mappings: list[NetworkMappingItem] | None = None
    storage_mappings: list[StorageMappingItem] | None = None
    namespace_mappings: list[NamespaceMappingItem] | NamespaceStrategy | None = None


class ResourceMappingRead(BaseModel):
    """Read-side schema. Decouples from ResourceMappingBase because the
    DB column stores raw JSON (list-or-dict), and we want to surface
    that without re-validating it through the create/update unions —
    callers downstream may have written shapes we don't recognize and
    we don't want to fail the read."""

    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    name: str
    vcenter_source_id: int
    ocp_target_id: int
    network_mappings: _DictRowList = Field(default_factory=list)
    storage_mappings: _DictRowList = Field(default_factory=list)
    # JSON column — list (legacy) or dict (new strategy shape).
    namespace_mappings: list[dict] | dict = Field(default_factory=list)
    status: ResourceMappingStatus
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# LLM suggestion request/response
# ---------------------------------------------------------------------------
class MappingSuggestionResponse(BaseModel):
    suggestions: list[NetworkMappingItem | StorageMappingItem]
    rationale_summary: str = ""


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------
class PreflightCheckResponse(BaseModel):
    """Returned by POST /api/mappings/{id}/preflight. Reports every
    source resource the operator's inventory references and whether
    that resource has a corresponding mapping entry."""

    ok: bool
    target_status: Literal["active", "inactive", "error", "unknown"] = "unknown"
    unmapped_networks: list[str] = Field(default_factory=list)
    unmapped_datastores: list[str] = Field(default_factory=list)
    missing_storage_classes_on_target: list[str] = Field(default_factory=list)
    missing_networks_on_target: list[str] = Field(default_factory=list)
    missing_namespaces_on_target: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
