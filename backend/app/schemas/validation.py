from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ValidationResultRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    vm_id: int
    status: Literal["pass", "warn", "fail"]
    summary: str
    findings: list[dict]
    remediation: list[dict]
    diff: dict
    validated_at: datetime
