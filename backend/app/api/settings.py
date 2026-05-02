"""Settings + system-info endpoints powering the /settings page."""

from __future__ import annotations

import hashlib
from base64 import b64decode
from pathlib import Path

import paramiko
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings as app_config
from app.core.db import get_db
from app.core.fips import fips_status
from app.core.llm.factory import get_llm_backend
from app.core.scheduler import next_run_time, reschedule_baseline_job
from app.models.settings import AppSettings
from app.schemas.settings import (
    AppSettingsRead,
    AppSettingsUpdate,
    FIPSStatus,
    LLMBackendInfo,
    OllamaModelsResponse,
    SSHPublicKey,
)

settings_router = APIRouter(tags=["settings"])
system_router = APIRouter(tags=["system"])

_SETTINGS_ID = 1


def _get_or_create_settings(db: Session) -> AppSettings:
    row = db.get(AppSettings, _SETTINGS_ID)
    if row is None:
        row = AppSettings(id=_SETTINGS_ID)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _settings_payload(row: AppSettings) -> dict:
    """Compose the response body — model fields + live scheduler state."""
    body = AppSettingsRead.model_validate(row).model_dump(mode="json")
    nxt = next_run_time()
    body["next_run_at"] = nxt.isoformat() if nxt is not None else None
    return body


@settings_router.get("", response_model=AppSettingsRead)
def get_settings(db: Session = Depends(get_db)) -> dict:
    row = _get_or_create_settings(db)
    return _settings_payload(row)


@settings_router.put("", response_model=AppSettingsRead)
def update_settings(payload: AppSettingsUpdate, db: Session = Depends(get_db)) -> dict:
    row = _get_or_create_settings(db)
    fields = payload.model_dump(exclude_unset=True)
    for k, v in fields.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    # If the cron preset changed, retune the running scheduler. Safe no-op
    # in tests where the scheduler was never started.
    if "schedule_preset" in fields:
        reschedule_baseline_job(row.schedule_preset)
    return _settings_payload(row)


@system_router.get("/ssh-public-key", response_model=SSHPublicKey)
def ssh_public_key() -> dict:
    """Return the OpenSSH public key VirtValidate uses. Never the private key.

    Polymorphic across Ed25519 / RSA / ECDSA — federal deployments
    running in FIPS mode use RSA-3072 or ECDSA P-384 and must still see
    their public key here, not just Ed25519.
    """
    priv_path = Path(app_config.ssh_key_path)
    pub_path = priv_path.with_suffix(priv_path.suffix + ".pub")

    raw: str | None = None
    if pub_path.is_file():
        raw = pub_path.read_text(encoding="utf-8").strip()
    elif priv_path.is_file():
        last_err: paramiko.SSHException | None = None
        for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
            try:
                key = cls.from_private_key_file(str(priv_path))
                raw = f"{key.get_name()} {key.get_base64()}"
                break
            except paramiko.SSHException as e:
                last_err = e
                continue
        if raw is None:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to load SSH key (Ed25519/RSA/ECDSA): {last_err}",
            )

    if not raw:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No SSH key found at {priv_path}. Generate one with "
                "`ssh-keygen -t ed25519 -f /app/keys/id_ed25519`."
            ),
        )

    parts = raw.split()
    key_type = parts[0] if parts else "ssh-ed25519"
    fingerprint: str | None = None
    if len(parts) >= 2:
        try:
            blob = b64decode(parts[1])
            fingerprint = "SHA256:" + (
                hashlib.sha256(blob).digest().hex()  # full hex; UI can truncate
            )
        except (ValueError, TypeError):
            fingerprint = None

    return {"public_key": raw, "fingerprint": fingerprint, "type": key_type}


@system_router.get("/ollama-models", response_model=OllamaModelsResponse)
def ollama_models() -> dict:
    """List models available on the active LLM backend.

    Endpoint name predates the multi-backend refactor; the response now
    routes through the backend's ``list_models()`` so KServe deployments
    return their model and Ollama deployments return everything pulled.
    Each entry carries just the ``name`` so the UI's existing dropdown
    works without changes.
    """
    backend = get_llm_backend()
    names = backend.list_models() or []
    return {"models": [{"name": n} for n in names]}


@system_router.get("/fips-status", response_model=FIPSStatus)
def fips_status_endpoint() -> dict:
    """FIPS 140-3 compliance posture — configured + detected + per-op status.

    Read-only — FIPS is a deployment decision controlled by the
    ``FIPS_MODE`` env var. The Settings UI surfaces this to operators
    so federal reviewers can audit the appliance's posture without
    shelling into the host.
    """
    return fips_status()


@system_router.get("/llm-info", response_model=LLMBackendInfo)
def llm_info() -> dict:
    """Active LLM backend snapshot — what's wired up + live status.

    The Settings UI uses this to render a read-only backend panel.
    Switching backends is a deployment decision (env var); the UI must
    not allow editing it. ``health.status`` reflects a live probe so
    operators see whether the configured endpoint is actually reachable.
    """
    backend = get_llm_backend()
    return {
        "config": backend.info(),
        "health": backend.health_check_sync(),
    }
