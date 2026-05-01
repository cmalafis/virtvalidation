from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.network_review import (
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
    FindingTriage,
    NetworkReviewStatus,
)


class NetworkReviewCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    customer_notes: str | None = Field(default=None, max_length=200_000)
    proposed_yaml: str | None = Field(default=None, max_length=200_000)


class NetworkReviewNotesUpdate(BaseModel):
    customer_notes: str = Field(default="", max_length=200_000)


class NetworkReviewYamlUpdate(BaseModel):
    proposed_yaml: str = Field(default="", max_length=200_000)


class NetworkFindingTriageUpdate(BaseModel):
    triage: Literal["open", "accepted", "dismissed"]


class NetworkFindingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: FindingCategory
    severity: FindingSeverity
    confidence: FindingConfidence
    triage: FindingTriage
    title: str
    description: str
    source_evidence: str
    proposed_evidence: str
    recommendation: str
    created_at: datetime


class NetworkReviewSummary(BaseModel):
    """Lightweight row for the index page."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: NetworkReviewStatus
    finding_count: int = 0
    severity_counts: dict[str, int] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    last_analyzed_at: datetime | None = None


class NetworkReviewRead(BaseModel):
    """Full review with embedded findings — used by the detail view."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    status: NetworkReviewStatus
    customer_notes: str
    proposed_yaml: str
    analysis_results: dict
    last_analyzed_at: datetime | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime
    findings: list[NetworkFindingRead] = Field(default_factory=list)
