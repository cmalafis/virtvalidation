"""Schemas for the read-only per-command audit surface."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CommandAuditRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vm_id: int | None
    host: str
    run_type: str
    run_id: int | None
    command: str
    exit_status: int | None
    stdout_byte_count: int
    stdout_sha256: str | None
    stdout_truncated: str | None
    duration_ms: int
    blocked: bool
    started_at: datetime


class CommandAuditListResponse(BaseModel):
    items: list[CommandAuditRead]
    total: int
    skip: int
    limit: int
