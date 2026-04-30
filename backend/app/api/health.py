"""Health-check endpoints for the system tab on the settings page.

Air-gapped by design: every probe targets a service inside the local
podman network (Ollama, Postgres) — never the public internet.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db

router = APIRouter(tags=["health"])

_OLLAMA_TIMEOUT_SECONDS = 5.0
_API_VERSION = "0.1.0"


@router.get("/ollama")
def ollama_health() -> dict:
    """Probe the local Ollama server via /api/tags."""
    host = settings.ollama_host.rstrip("/")
    try:
        with httpx.Client(timeout=_OLLAMA_TIMEOUT_SECONDS) as c:
            r = c.get(f"{host}/api/tags")
            r.raise_for_status()
            data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        return {"status": "offline", "error": str(e)}

    available = [
        m["name"] for m in (data.get("models") or []) if isinstance(m, dict) and m.get("name")
    ]
    return {
        "status": "online",
        "model": settings.ollama_model,
        "available_models": available,
    }


@router.get("/postgres")
def postgres_health(db: Session = Depends(get_db)) -> dict:
    """Run a SELECT 1 against the configured database."""
    try:
        db.execute(text("SELECT 1"))
        return {"status": "online"}
    except SQLAlchemyError as e:
        return {"status": "offline", "error": str(e)}


@router.get("/full")
def full_health(db: Session = Depends(get_db)) -> dict:
    """Combined status across the API and every backing dependency."""
    return {
        "api": {"status": "online", "version": _API_VERSION},
        "ollama": ollama_health(),
        "postgres": postgres_health(db),
    }
