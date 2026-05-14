"""Settings + system-info endpoints powering the /settings page."""

from __future__ import annotations

import hashlib
from base64 import b64decode
from pathlib import Path

import paramiko
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.config import settings as app_config
from app.core.db import get_db
from app.core.fips import FIPSViolation, fips_status
from app.core.llm.factory import get_llm_backend
from app.core.scheduler import next_run_time, reschedule_baseline_job
from app.core.ssh_key import (
    KeyExistsError,
    KeyMissingError,
    UnsupportedAlgorithmError,
)
from app.core.ssh_key import (
    expected_path as ssh_expected_path,
)
from app.core.ssh_key import (
    generate as ssh_generate,
)
from app.core.ssh_key import (
    load_key_info as ssh_load_key_info,
)
from app.core.ssh_key import (
    normalize_algorithm as ssh_normalize_algorithm,
)
from app.core.ssh_key import (
    rotate as ssh_rotate,
)
from app.models.settings import AppSettings
from app.schemas.settings import (
    AppSettingsRead,
    AppSettingsUpdate,
    FIPSStatus,
    LLMBackendInfo,
    OllamaModelsResponse,
    SSHKeyGenerateRequest,
    SSHKeyGenerateResponse,
    SSHKeyRotateResponse,
    SSHKeyStatus,
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


def _find_existing_ssh_key():
    """Look across every algorithm's expected path and return the first hit.

    The legacy SSH_KEY_PATH (``/app/keys/id_ed25519``) is checked first
    by way of the configured default, then RSA / ECDSA. Returns the
    KeyInfo + the resolved Path so callers can use both without
    re-walking.
    """
    configured = ssh_normalize_algorithm(None)
    candidates = [configured] + [a for a in ("ed25519", "rsa", "ecdsa") if a != configured]
    for algo in candidates:
        path = ssh_expected_path(algo)
        info = ssh_load_key_info(path)
        if info is not None:
            return info, path
    return None, ssh_expected_path(configured)


@system_router.get("/ssh-key", response_model=SSHKeyStatus)
def ssh_key_status(request: Request, db: Session = Depends(get_db)) -> dict:
    """Return the appliance SSH key in a wrapped ``{status, ...}`` shape.

    Distinguishes "missing" from "exists" without forcing the UI to
    parse error bodies — the previous 404-on-missing contract made the
    Settings page render a CLI command instead of a generate button.
    The Settings UI's three-state SSH panel keys off this response.

    "ssh.key_viewed" is recorded at INFO level so federal audit trails
    can show every time the public key was surfaced (the audit
    middleware's default-action map would emit ``api.get``, which
    isn't specific enough for compliance review).
    """
    info, path = _find_existing_ssh_key()
    actor = request.headers.get("x-actor", "user")
    if info is not None:
        record_audit(
            db,
            action="ssh.key_viewed",
            actor=actor,
            resource_type="ssh_key",
            details={
                "algorithm": info.algorithm,
                "fingerprint": info.fingerprint,
            },
        )
        db.commit()
        request.state.skip_audit_log = True
        return {
            "status": "exists",
            "algorithm": info.algorithm,
            "fingerprint": info.fingerprint,
            "public_key": info.public_key,
            "created_at": info.created_at,
        }
    request.state.skip_audit_log = True
    return {
        "status": "missing",
        "configured_algorithm": ssh_normalize_algorithm(None),
        "expected_path": str(path),
    }


@system_router.post(
    "/ssh-key/generate",
    response_model=SSHKeyGenerateResponse,
    status_code=201,
)
def ssh_key_generate(
    request: Request,
    payload: SSHKeyGenerateRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Materialize a new keypair on the appliance.

    Refuses when a key already exists — operators rotate via the
    sibling endpoint, which audits the old/new fingerprint pair
    instead of silently replacing.
    """
    actor = request.headers.get("x-actor", "user")
    try:
        algo = ssh_normalize_algorithm(payload.algorithm)
    except UnsupportedAlgorithmError as e:
        request.state.skip_audit_log = True
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        info = ssh_generate(algo)
    except FIPSViolation as e:
        request.state.skip_audit_log = True
        raise HTTPException(status_code=400, detail=str(e)) from e
    except KeyExistsError as e:
        request.state.skip_audit_log = True
        raise HTTPException(
            status_code=409,
            detail=(
                f"SSH key already exists at {e.path}. "
                "Use POST /api/system/ssh-key/rotate to replace it."
            ),
        ) from e

    record_audit(
        db,
        action="ssh.key_generated",
        actor=actor,
        resource_type="ssh_key",
        details={
            "algorithm": info.algorithm,
            "fingerprint": info.fingerprint,
            "path": info.path,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return {
        "algorithm": info.algorithm,
        "fingerprint": info.fingerprint,
        "public_key": info.public_key,
        "created_at": info.created_at,
    }


@system_router.post(
    "/ssh-key/rotate",
    response_model=SSHKeyRotateResponse,
)
def ssh_key_rotate(
    request: Request,
    payload: SSHKeyGenerateRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Replace the existing appliance keypair with a freshly generated one.

    The old key is renamed to ``.bak.<unix-ts>`` (both private + public)
    so a botched rotation can be rolled back. The audit log captures
    old + new fingerprints so federal reviewers can correlate the
    rotation with the authorized_keys update on every managed VM.
    """
    actor = request.headers.get("x-actor", "user")
    try:
        algo = ssh_normalize_algorithm(payload.algorithm)
    except UnsupportedAlgorithmError as e:
        request.state.skip_audit_log = True
        raise HTTPException(status_code=400, detail=str(e)) from e

    try:
        old_info, new_info, backups = ssh_rotate(algo)
    except FIPSViolation as e:
        request.state.skip_audit_log = True
        raise HTTPException(status_code=400, detail=str(e)) from e
    except KeyMissingError as e:
        request.state.skip_audit_log = True
        raise HTTPException(
            status_code=404,
            detail=(
                f"No SSH key found at {e.path}. " "Use POST /api/system/ssh-key/generate first."
            ),
        ) from e

    record_audit(
        db,
        action="ssh.key_rotated",
        actor=actor,
        resource_type="ssh_key",
        details={
            "old_algorithm": old_info.algorithm,
            "old_fingerprint": old_info.fingerprint,
            "new_algorithm": new_info.algorithm,
            "new_fingerprint": new_info.fingerprint,
            "backups": backups,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return {
        "algorithm": new_info.algorithm,
        "fingerprint": new_info.fingerprint,
        "public_key": new_info.public_key,
        "created_at": new_info.created_at,
        "previous_fingerprint": old_info.fingerprint,
        "backups": backups,
        "warning": (
            "Old key has been backed up to the listed paths. Update "
            "authorized_keys on every managed VM with the new public "
            "key before existing SSH sessions are no longer functional."
        ),
    }


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


@system_router.get("/llm-usage")
def llm_usage(hours: int = 24, db: Session = Depends(get_db)) -> dict:
    """Rolled-up usage metrics for the admin dashboard.

    Returns:
        - operation-level call counts + token totals over the last
          ``hours`` window
        - validation-specific tier distribution + cache hit rate
        - estimated USD cost (configurable rate; 0 by default for
          locally-hosted Ollama)
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    from app.core.config import settings as cfg
    from app.core.validation_cache import stats as cache_stats
    from app.models.audit import AuditLog
    from app.models.llm_usage import LLMUsage

    since = datetime.now(timezone.utc) - timedelta(hours=max(1, hours))
    rows = list(db.scalars(select(LLMUsage).where(LLMUsage.created_at >= since)).all())

    by_operation: dict[str, dict] = {}
    total_in = 0
    total_out = 0
    for r in rows:
        bucket = by_operation.setdefault(
            r.operation,
            {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        bucket["calls"] += 1
        bucket["input_tokens"] += r.input_tokens or 0
        bucket["output_tokens"] += r.output_tokens or 0
        bucket["total_tokens"] += r.total_tokens or 0
        total_in += r.input_tokens or 0
        total_out += r.output_tokens or 0

    # Pull recent validation.completed audit rows to derive tier
    # distribution + cache-hit rate. The rows carry tier / cached /
    # needs_manual_review in their details JSON.
    audit_rows = list(
        db.scalars(
            select(AuditLog)
            .where(AuditLog.action == "validation.completed")
            .where(AuditLog.timestamp >= since)
        ).all()
    )
    tier_counts = {"tier1": 0, "tier2": 0, "tier3": 0}
    cached = 0
    manual = 0
    for a in audit_rows:
        details = a.details or {}
        tier = details.get("tier")
        if tier in tier_counts:
            tier_counts[tier] += 1
        if details.get("cached"):
            cached += 1
        if details.get("needs_manual_review"):
            manual += 1

    validations_total = sum(tier_counts.values())
    cache_hit_rate = round(100 * cached / validations_total, 1) if validations_total else 0.0

    cost_usd = total_in * (cfg.llm_cost_per_million_input_tokens / 1_000_000.0) + total_out * (
        cfg.llm_cost_per_million_output_tokens / 1_000_000.0
    )

    return {
        "window_hours": hours,
        "since": since.isoformat(),
        "by_operation": by_operation,
        "totals": {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "estimated_cost_usd": round(cost_usd, 4),
            "rate_per_1m_input": cfg.llm_cost_per_million_input_tokens,
            "rate_per_1m_output": cfg.llm_cost_per_million_output_tokens,
        },
        "validations": {
            "total": validations_total,
            "tier_distribution": tier_counts,
            "cached_count": cached,
            "cache_hit_rate_percent": cache_hit_rate,
            "needs_manual_review_count": manual,
        },
        "cache": cache_stats(db),
    }
