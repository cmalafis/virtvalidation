from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.models.validation import ValidationStatus


class ValidationResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    vm_id: int
    status: ValidationStatus
    summary: str
    findings: list[dict]
    remediation: list[dict]
    diff: dict
    validated_at: datetime


class LatestValidationResponse(BaseModel):
    """Wrapper so a missing validation is a 200 with `validation: null`
    instead of a 404 — the UI then renders an empty state directly without
    treating "no validation yet" as an error."""

    validation: ValidationResultRead | None = None


class ValidationTaskRead(BaseModel):
    task_id: str
    vm_id: int
    status: Literal["running", "completed", "failed"]
    current_step: Literal[
        "queued", "ssh_collecting", "llm_reasoning", "storing", "completed", "failed"
    ]
    progress_percent: int
    started_at: datetime
    completed_at: datetime | None = None
    validation_id: int | None = None
    verdict: Literal["pass", "warn", "fail"] | None = None
    error: str | None = None


class BulkValidationSpawn(BaseModel):
    vm_id: int
    task_id: str


class BulkValidationResult(BaseModel):
    spawned: list[BulkValidationSpawn]
    skipped: list[dict]  # [{"vm_id": int, "reason": str}, ...]
