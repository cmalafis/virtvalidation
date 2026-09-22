from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

ImportMode = Literal["create_only", "upsert"]


class ImportJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    filename: str
    file_size: int
    file_sha256: str | None = None
    mode: str
    status: str
    current_sheet: str | None = None
    progress_message: str | None = None
    rows_total: int | None = None
    rows_read: int = 0
    rows_valid: int = 0
    rows_rejected: int = 0
    rows_warned: int = 0
    created_count: int = 0
    updated_count: int = 0
    unchanged_count: int = 0
    marked_missing_count: int = 0
    detected_vcenters: list[dict] = Field(default_factory=list)
    vcenter_mapping: dict[str, int] = Field(default_factory=dict)
    default_vcenter_id: int | None = None
    result: dict = Field(default_factory=dict)
    error_message: str | None = None
    cancel_requested: bool = False
    actor: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress_percent(self) -> int | None:
        """NULL when the workbook doesn't declare its row counts — the UI
        then shows rows processed rather than inventing a percentage."""
        if self.status == "completed":
            return 100
        if not self.rows_total:
            return None
        return max(0, min(99, int(self.rows_read * 100 / self.rows_total)))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def elapsed_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        end = self.completed_at or datetime.now(timezone.utc)
        start = self.started_at
        if start.tzinfo is None:  # SQLite hands back naive datetimes
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return round((end - start).total_seconds(), 2)


class ImportJobListResponse(BaseModel):
    items: list[ImportJobRead]
    total: int
    skip: int
    limit: int


class ImportStartRequest(BaseModel):
    # detected hostname ("" = rows with no vCenter column) → vcenter_source id
    vcenter_mapping: dict[str, int] = Field(default_factory=dict)
    default_vcenter_id: int | None = None
    mode: ImportMode | None = None


class ImportRejectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    sheet: str
    row_number: int
    vm_name: str | None = None
    severity: str
    reason: str


class ImportRejectListResponse(BaseModel):
    items: list[ImportRejectRead]
    total: int
    skip: int
    limit: int
