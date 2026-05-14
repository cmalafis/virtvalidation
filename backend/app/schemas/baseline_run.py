"""Pydantic schemas for the wave-scoped BaselineRun + Baseline resources."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.baseline_run import BaselineRunStatus, VMCollectionStatus


class BaselineRunCreate(BaseModel):
    ssh_key_id: int = Field(...)


class BaselineRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    baseline_run_id: int
    vm_id: int
    status: VMCollectionStatus
    collected_data: dict | None = None
    probe_catalog_version: str | None = None
    probes_run: list[str] | None = None
    host_key_fingerprint: str | None = None
    failure_category: str | None = None
    failure_detail: str | None = None
    collection_started_at: datetime | None = None
    collection_completed_at: datetime | None = None
    captured_at: datetime


class BaselineRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    plan_id: int
    wave_number: int
    ssh_key_id: int
    status: BaselineRunStatus
    total_vms: int
    captured_vms: int
    failed_vms: int
    progress_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    baselines: list[BaselineRead] = Field(default_factory=list)


class BaselineRunAccepted(BaseModel):
    """Body returned by POST endpoints that kick off a run.

    Mirrors the existing async pattern: ``id`` so the UI can immediately
    start polling, ``status_url`` as a convenience for direct copy-paste.
    """

    baseline_run_id: int
    status: BaselineRunStatus
    status_url: str
