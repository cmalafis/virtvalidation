from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.limits import MAX_VMS_PER_RVTOOLS_IMPORT
from app.models.vcenter import ClassificationLevel, VCenterStatus


class VCenterSourceBase(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    hostname: str = Field(min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    site: str | None = Field(default=None, max_length=64)
    classification_level: ClassificationLevel = ClassificationLevel.unclassified
    status: VCenterStatus = VCenterStatus.active
    default_target_namespace: str | None = Field(default=None, max_length=253)
    default_target_storage_class: str | None = Field(default=None, max_length=253)
    notes: str | None = Field(default=None, max_length=2048)


class VCenterSourceCreate(VCenterSourceBase):
    pass


class VCenterSourceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    hostname: str | None = Field(default=None, min_length=1, max_length=255)
    region: str | None = Field(default=None, max_length=64)
    site: str | None = Field(default=None, max_length=64)
    classification_level: ClassificationLevel | None = None
    status: VCenterStatus | None = None
    default_target_namespace: str | None = Field(default=None, max_length=253)
    default_target_storage_class: str | None = Field(default=None, max_length=253)
    notes: str | None = Field(default=None, max_length=2048)


class VCenterSourceRead(VCenterSourceBase):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    created_at: datetime
    updated_at: datetime
    # Computed by the API layer — number of VMs currently tagged with
    # this vCenter as their source. Surfaced on the list view so
    # operators can spot empty registrations at a glance.
    vm_count: int = 0


# ---------------------------------------------------------------------------
# RVTools delta-upload preview payload — parsed VM list goes in,
# {new, updated, removed, unchanged} comes back without committing.
# ---------------------------------------------------------------------------
class RVToolsVMRow(BaseModel):
    """The minimal shape the delta-detector reads.

    Mirrors what the frontend's parseXLSX produces; extra fields are
    accepted and ignored by the validator.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    source_hostname: str | None = Field(default=None, max_length=255)
    ip_address: str | None = Field(default=None, max_length=45)
    os_family: str | None = Field(default=None, max_length=64)
    role: str | None = Field(default=None, max_length=64)
    environment: str | None = Field(default=None, max_length=64)
    owner: str | None = Field(default=None, max_length=128)
    application_hint: str | None = Field(default=None, max_length=128)
    vsphere_networks: list[str] = Field(default_factory=list)
    vsphere_datastores: list[str] = Field(default_factory=list)
    # The RVTools vCenter column for this row. Populated by the
    # frontend parser when the upload contains the column; lets the
    # upload-multi-vcenter endpoint auto-route VMs without an
    # operator-supplied per-row override.
    source_vcenter_hostname: str | None = Field(default=None, max_length=255)
    # Placement metadata the environment detector reads. These used to be
    # dropped here (``extra="ignore"``), which left every VM imported
    # through the JSON endpoints with ``environment = NULL``.
    vsphere_cluster: str | None = Field(default=None, max_length=255)
    vsphere_folder: str | None = Field(default=None, max_length=512)
    custom_attributes: dict[str, str] = Field(default_factory=dict)


class RVToolsDeltaRequest(BaseModel):
    vms: list[RVToolsVMRow] = Field(min_length=1, max_length=MAX_VMS_PER_RVTOOLS_IMPORT)


class RVToolsDeltaItem(BaseModel):
    name: str
    diff: dict | None = None  # populated for "updated" entries


class RVToolsDeltaResponse(BaseModel):
    new: list[RVToolsDeltaItem]
    updated: list[RVToolsDeltaItem]
    removed: list[RVToolsDeltaItem]
    unchanged: list[RVToolsDeltaItem]
    summary: dict[str, int]


class RVToolsImportResult(BaseModel):
    """Sync-mode import result.

    For uploads under the background-task threshold the import endpoint
    returns this body directly (HTTP 200). Above the threshold it returns
    202 + :class:`RVToolsImportTaskRead` and the operator polls.
    """

    created: int = 0
    updated: int = 0
    marked_missing: int = 0
    unchanged: int = 0
    errors: list[str] = Field(default_factory=list)


class RVToolsImportTaskRead(BaseModel):
    """Async-mode handle for large imports."""

    task_id: str
    status: Literal["running", "completed", "failed"]
    progress_percent: int = 0
    started_at: datetime
    completed_at: datetime | None = None
    result: RVToolsImportResult | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Categorization (Level 1) — output schemas
# ---------------------------------------------------------------------------
class CategorizationGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    kind: Literal["application", "environment", "business_unit", "other"]
    name: str
    description: str | None
    vm_count: int


class CategorizationTaskRead(BaseModel):
    task_id: str
    source_vcenter_id: int
    status: Literal["running", "completed", "failed"]
    current_step: Literal[
        "queued", "loading_inventory", "calling_llm", "persisting", "completed", "failed"
    ]
    progress_percent: int
    batches_total: int
    batches_complete: int
    started_at: datetime
    completed_at: datetime | None = None
    groups_created: int | None = None
    error: str | None = None
