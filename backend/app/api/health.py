"""Health-check endpoints for the system tab on the settings page.

LLM probe is delegated to the configured backend so the report
reflects whichever inference engine is wired up (Ollama by default,
KServe in RHOAI deployments, vLLM in v1.0.0). Postgres still has its
own probe — same connection pool the API uses anyway.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.fips import fips_status
from app.core.llm.factory import get_llm_backend

router = APIRouter(tags=["health"])

_API_VERSION = "0.1.0"


@router.get("/llm")
def llm_health() -> dict:
    """Probe the configured LLM backend.

    Routes through :meth:`LLMBackend.health_check_sync` so Ollama,
    KServe, and vLLM all report through the same shape.
    """
    backend = get_llm_backend()
    return backend.health_check_sync()


@router.get("/ollama")
def ollama_health() -> dict:
    """Back-compat alias for ``/health/llm``.

    Pre-refactor clients (older Settings UI builds) hit this path. The
    response is the same backend-agnostic envelope so they degrade
    gracefully when the deployment isn't actually using Ollama.
    """
    return llm_health()


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
    """Combined status across the API and every backing dependency.

    The new ``components`` block is the canonical shape going forward;
    the top-level ``ollama`` / ``postgres`` keys are kept temporarily
    for the old UI build that still reads them directly.
    """
    pg = postgres_health(db)
    llm = llm_health()
    return {
        "api": {"status": "online", "version": _API_VERSION},
        "components": {
            "database": pg,
            "llm": llm,
        },
        # FIPS posture — surfaced here so federal reviewers and load
        # balancer health probes both see the compliance status in
        # one round-trip.
        "fips": fips_status(),
        # Deprecated — remove once the dashboard build catches up.
        "ollama": llm,
        "postgres": pg,
    }
