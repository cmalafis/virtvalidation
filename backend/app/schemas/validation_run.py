"""Pydantic schemas for the wave-scoped ValidationRun + VMValidation
resources."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.validation_run import ValidationRunStatus, VMValidationVerdict


class ValidationRunCreate(BaseModel):
    ssh_key_id: int = Field(...)


class VMValidationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    validation_run_id: int
    vm_id: int
    baseline_id: int | None = None
    verdict: VMValidationVerdict
    collected_data: dict | None = None
    diff_result: dict | None = None
    host_key_changed: bool
    failure_category: str | None = None
    failure_detail: str | None = None
    validated_at: datetime


class ValidationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    plan_id: int
    wave_number: int
    ssh_key_id: int
    status: ValidationRunStatus
    total_vms: int
    passed_vms: int
    warned_vms: int
    failed_vms: int
    unreachable_vms: int
    progress_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    validations: list[VMValidationRead] = Field(default_factory=list)


class ValidationRunAccepted(BaseModel):
    validation_run_id: int
    status: ValidationRunStatus
    status_url: str


class RevokeValidationKeyRequest(BaseModel):
    ssh_key_id: int = Field(...)
    force: bool = Field(default=False)
