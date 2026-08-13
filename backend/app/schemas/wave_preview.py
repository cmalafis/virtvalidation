"""Schema for the wave dry-run preview — what WOULD run, without connecting."""

from __future__ import annotations

from pydantic import BaseModel


class WaveVMPreview(BaseModel):
    vm_id: int
    name: str
    host: str
    username: str
    os_family: str | None
    environment: str | None
    # The read-only commands that would run, best-effort from the declared
    # os_family (actual dispatch happens against the detected OS at run time).
    commands: list[str]


class WavePreviewResponse(BaseModel):
    plan_id: int
    wave_number: int
    vm_count: int
    # Whether the authorization gate applies to this wave, and why.
    requires_authorization: bool
    authorization_reason: str
    # Global kill-switch state — false means a run would be refused (503).
    ssh_operations_enabled: bool
    vms: list[WaveVMPreview]
