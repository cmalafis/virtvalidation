from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.network_review import (
    FindingConfidence,
    FindingSeverity,
    FindingTriage,
)
from app.models.storage_review import StorageFindingCategory, StorageReviewStatus


class StorageReviewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    customer_notes: str | None = Field(default=None, max_length=200_000)
    proposed_yaml: str | None = Field(default=None, max_length=200_000)


class StorageReviewNotesUpdate(BaseModel):
    customer_notes: str = Field(default="", max_length=200_000)


class StorageReviewYamlUpdate(BaseModel):
    proposed_yaml: str = Field(default="", max_length=200_000)


class StorageFindingTriageUpdate(BaseModel):
    triage: Literal["open", "accepted", "dismissed"]


class StorageFindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: StorageFindingCategory
    severity: FindingSeverity
    confidence: FindingConfidence
    triage: FindingTriage
    title: str
    description: str
    source_evidence: str
    proposed_evidence: str
    recommendation: str
    created_at: datetime


class StorageReviewSummary(BaseModel):
    """Lightweight row for the index page."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: StorageReviewStatus
    finding_count: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    last_analyzed_at: datetime | None = None


class StorageReviewRead(BaseModel):
    """Full review with embedded findings — used by the detail view."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: StorageReviewStatus
    customer_notes: str
    proposed_yaml: str
    analysis_results: dict
    last_analyzed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    findings: list[StorageFindingRead] = Field(default_factory=list)
