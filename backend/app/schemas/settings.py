from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.settings import SchedulePreset, SSHHostKeyPolicy


class AppSettingsRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ollama_model: str
    schedule_preset: SchedulePreset
    ssh_host_key_policy: SSHHostKeyPolicy
    updated_at: datetime
    # Populated from the running APScheduler instance — None when the
    # scheduler isn't running (tests, freshly booted process, etc.).
    # The dashboard formats this as "Next collection: in 3h 42m".
    next_run_at: datetime | None = None


class AppSettingsUpdate(BaseModel):
    ollama_model: str | None = Field(default=None, min_length=1, max_length=128)
    schedule_preset: SchedulePreset | None = None
    ssh_host_key_policy: SSHHostKeyPolicy | None = None


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


class SSHKeyStatus(BaseModel):
    """Response shape for ``GET /api/system/ssh-key``.

    Wraps the old SSHPublicKey response in a ``status`` envelope so the
    Settings UI can distinguish "key not generated yet" from "API
    error". Status is one of:
      - ``"exists"`` — key files present, fields populated.
      - ``"missing"`` — files absent, ``configured_algorithm`` +
        ``expected_path`` echoed back so the UI can render the
        generate-now form.
    """

    status: str
    algorithm: str | None = None
    fingerprint: str | None = None
    public_key: str | None = None
    created_at: datetime | None = None
    configured_algorithm: str | None = None
    expected_path: str | None = None


class SSHKeyGenerateRequest(BaseModel):
    # Algorithm choice. None falls back to the SSH_KEY_ALGORITHM env var
    # (default: ed25519). Validated server-side against
    # SUPPORTED_ALGORITHMS + the FIPS gate.
    algorithm: str | None = Field(default=None, max_length=16)


class SSHKeyGenerateResponse(BaseModel):
    algorithm: str
    fingerprint: str
    public_key: str
    created_at: datetime


class SSHKeyRotateResponse(BaseModel):
    algorithm: str
    fingerprint: str
    public_key: str
    created_at: datetime
    previous_fingerprint: str
    backups: list[str]
    warning: str


# ---------------------------------------------------------------------------
# LLM backend info — surfaced read-only on the Settings page so operators
# can see which inference engine the deployment is wired up against.
# ---------------------------------------------------------------------------
class LLMBackendConfig(BaseModel):
    backend: str  # "ollama" | "kserve" | "vllm"
    model: str | None = None
    endpoint: str | None = None


class LLMBackendHealth(BaseModel):
    status: str  # "online" | "offline"
    backend: str
    model: str | None = None
    endpoint: str | None = None
    latency_ms: int | None = None
    details: dict | None = None


class LLMBackendInfo(BaseModel):
    config: LLMBackendConfig
    health: LLMBackendHealth


# ---------------------------------------------------------------------------
# FIPS 140-3 status — exposed at /api/system/fips-status and embedded in
# /api/health/full so federal reviewers can audit the posture in one shot.
# ---------------------------------------------------------------------------
class FIPSOperationStatus(BaseModel):
    name: str
    configured: str
    fips_approved: bool
    enforced: bool


class FIPSStatus(BaseModel):
    configured: bool
    detected: bool
    effective: bool
    mismatch_warning: str | None = None
    operations: list[FIPSOperationStatus]
