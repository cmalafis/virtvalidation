"""Settings + system-info endpoints powering the /settings page."""

from __future__ import annotations

import hashlib
from base64 import b64decode
from pathlib import Path

import httpx
import paramiko
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings as app_config
from app.core.db import get_db
from app.core.scheduler import next_run_time, reschedule_baseline_job
from app.models.settings import AppSettings
from app.schemas.settings import (
    AppSettingsRead,
    AppSettingsUpdate,
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
    """Return the OpenSSH public key VirtValidate uses. Never the private key."""
    priv_path = Path(app_config.ssh_key_path)
    pub_path = priv_path.with_suffix(priv_path.suffix + ".pub")

    raw: str | None = None
    if pub_path.is_file():
        raw = pub_path.read_text(encoding="utf-8").strip()
    elif priv_path.is_file():
        try:
            key = paramiko.Ed25519Key.from_private_key_file(str(priv_path))
            raw = f"{key.get_name()} {key.get_base64()}"
        except paramiko.SSHException as e:
            raise HTTPException(status_code=500, detail=f"Failed to load SSH key: {e}") from e

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
    """List models currently pulled in the local Ollama instance."""
    host = app_config.ollama_host.rstrip("/")
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{host}/api/tags")
            r.raise_for_status()
            body = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Ollama unavailable at {host}: {e}") from e
    except ValueError as e:
        raise HTTPException(status_code=502, detail=f"Ollama returned non-JSON: {e}") from e

    return {"models": body.get("models", [])}
