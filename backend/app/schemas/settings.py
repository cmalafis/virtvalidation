from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.settings import SchedulePreset


class AppSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ollama_model: str
    schedule_preset: SchedulePreset
    updated_at: datetime


class AppSettingsUpdate(BaseModel):
    ollama_model: str | None = Field(default=None, min_length=1, max_length=128)
    schedule_preset: SchedulePreset | None = None


class HealthStatus(BaseModel):
    status: str  # "online" | "offline"
    host: str | None = None
    version: str | None = None
    latency_ms: int | None = None
    error: str | None = None


class OllamaModel(BaseModel):
    name: str
    size: int | None = None
    modified_at: str | None = None


class OllamaModelsResponse(BaseModel):
    models: list[OllamaModel]


class SSHPublicKey(BaseModel):
    public_key: str
    fingerprint: str | None = None
    type: str = "ssh-ed25519"
