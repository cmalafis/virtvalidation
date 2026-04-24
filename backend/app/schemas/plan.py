from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class WaveRead(BaseModel):
    wave_number: int
    vm_ids: list[int]
    rationale: str
    estimated_risk: Literal["low", "medium", "high"]


class PlanCreate(BaseModel):
    vm_ids: list[int] = Field(min_length=1)


class PlanRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vm_ids: list[int]
    waves: list[WaveRead]
    summary: str | None
    model: str
    created_at: datetime
