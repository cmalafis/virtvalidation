"""Health-check endpoints for the system tab on the settings page."""

from __future__ import annotations

import time

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.schemas.settings import HealthStatus

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/ollama", response_model=HealthStatus)
def ollama_health() -> dict:
    """Probe the local Ollama server. Air-gapped — never calls external APIs."""
    start = time.monotonic()
    host = settings.ollama_host.rstrip("/")
    try:
        with httpx.Client(timeout=5.0) as c:
            r = c.get(f"{host}/api/version")
            r.raise_for_status()
            data = r.json()
        return {
            "status": "online",
            "host": host,
            "version": data.get("version"),
            "latency_ms": int((time.monotonic() - start) * 1000),
        }
    except (httpx.HTTPError, ValueError) as e:
        return {"status": "offline", "host": host, "error": str(e)}


@router.get("/postgres", response_model=HealthStatus)
def postgres_health(db: Session = Depends(get_db)) -> dict:
    """Run a SELECT 1 against the configured database."""
    start = time.monotonic()
    try:
        db.execute(text("SELECT 1"))
        return {
            "status": "online",
            "latency_ms": int((time.monotonic() - start) * 1000),
        }
    except SQLAlchemyError as e:
        return {"status": "offline", "error": str(e)}
