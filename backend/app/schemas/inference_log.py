"""Schemas for the read-only inference-log audit surface."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class InferenceLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    operation: str
    backend_type: str
    model: str
    method: str
    input_messages: list | None
    output_text: str | None
    detections: dict | None
    verdict: str | None
    latency_ms: int
    resource_type: str | None
    resource_id: int | None
    vm_id: int | None
    created_at: datetime


class InferenceLogListResponse(BaseModel):
    items: list[InferenceLogRead]
    total: int
    skip: int
    limit: int


class InferenceLogStats(BaseModel):
    """Roll-up for the dashboard: how many calls, and how often each
    fallback path fired. A non-trivial ``mechanical_fallback*`` share is the
    operator's signal that LLM quality (or auth, or a guardrail) is degraded."""

    total: int
    by_method: dict[str, int]
    by_operation: dict[str, int]
