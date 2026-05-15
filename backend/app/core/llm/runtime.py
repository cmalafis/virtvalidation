"""Runtime LLM backend resolution + connection testing.

The DB-backed singleton ``app_settings.active_llm_backend`` is the
authoritative answer to "which backend should we call right now?".
This module wraps that lookup with a short in-process TTL cache so
the planner's per-wave annotation (parallelized via asyncio.gather)
doesn't issue a DB round-trip per wave.

The cache holds only the *backend type string*, NOT the backend
instance — instance caching lives in
:func:`app.core.llm.factory.get_llm_backend_for_type`, which keeps
each type warm independently. Switching backends in the UI invalidates
the type cache locally; replicas pick up the new value within
``_TYPE_CACHE_TTL_SECONDS`` (60s).

Connection testing builds the backend for the requested type without
touching the active selection so an operator can verify a not-yet-
active backend works before flipping the switch.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.config import Settings
from app.core.config import settings as _module_settings
from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.factory import get_llm_backend_for_type
from app.core.llm.types import LLMBackendType

logger = logging.getLogger(__name__)

# How long a resolved active-backend type stays cached in-process.
# 60s is the upper bound on staleness after the operator flips the
# Settings UI on a multi-replica deployment; on the writing replica
# the invalidate() call drops the entry immediately.
_TYPE_CACHE_TTL_SECONDS = 60.0

_type_cache_lock = threading.Lock()
_cached_type: tuple[str, float] | None = None  # (backend_type, fetched_at_monotonic)


def invalidate() -> None:
    """Drop the cached active-backend type — call after writing
    ``app_settings.active_llm_backend`` so the next resolution re-reads
    the DB instead of returning the stale value."""
    global _cached_type
    with _type_cache_lock:
        _cached_type = None


def _fallback_backend_type() -> str:
    """The bootstrap value used when the DB row is missing OR the
    ``app_settings`` table itself doesn't exist yet (legacy DBs that
    haven't migrated, tests that build a partial schema). Falls back
    to the env-var value, then to "mock"."""
    return (_module_settings.llm_backend_type or "mock").lower()


def _read_active_backend_from_db(db: Session | None = None) -> str:
    """Look up ``app_settings.active_llm_backend``. Falls back to the
    bootstrap value if the row doesn't exist yet (race during first
    boot — the migration normally seeds it) OR if the table is missing
    (early test setup)."""
    from sqlalchemy.exc import OperationalError, ProgrammingError

    from app.models.settings import AppSettings  # avoid circular import

    # Resolve SessionLocal at call time so the test conftest's rebind
    # is honored. Importing the symbol at module load freezes the
    # binding to whatever the engine was at first import.
    own_session = db is None
    session = db or _db_module.SessionLocal()
    try:
        try:
            row = session.get(AppSettings, 1)
        except (OperationalError, ProgrammingError):
            # Table doesn't exist yet — common in tests that don't
            # create the full schema. Don't raise; the bootstrap value
            # is the right answer.
            return _fallback_backend_type()
        if row is None or row.active_llm_backend is None:
            return _fallback_backend_type()
        value = row.active_llm_backend
        return value.value if hasattr(value, "value") else str(value)
    finally:
        if own_session:
            session.close()


def get_active_backend_type(db: Session | None = None) -> str:
    """Return the currently-active backend type string, with a short
    in-process cache to avoid hitting the DB on every LLM call."""
    global _cached_type
    now = time.monotonic()
    with _type_cache_lock:
        if _cached_type is not None:
            value, fetched_at = _cached_type
            if now - fetched_at < _TYPE_CACHE_TTL_SECONDS:
                return value
    # DB read happens outside the lock so a slow lookup doesn't block
    # other resolvers. A racing concurrent reader will do an extra DB
    # round-trip — acceptable; both writes will agree.
    value = _read_active_backend_from_db(db)
    with _type_cache_lock:
        _cached_type = (value, now)
    return value


def get_active_backend(db: Session | None = None, cfg: Settings | None = None) -> LLMBackend:
    """Return the LLMBackend instance for the currently-active type.

    This is the resolver every orchestrator should call. The legacy
    :func:`app.core.llm.factory.get_llm_backend` reads the env var
    only — useful for the bootstrap path and for tests that want to
    pin a specific backend.
    """
    backend_type = get_active_backend_type(db)
    return get_llm_backend_for_type(backend_type, cfg)


# ---------------------------------------------------------------------------
# Backend availability — "is this backend's connection config present?"
# ---------------------------------------------------------------------------
def is_backend_configured(backend_type: str, cfg: Settings | None = None) -> bool:
    """Return True iff the named backend's *connection config* is
    sufficient to instantiate it. Operators can't pick a backend in
    the UI whose required config is missing — selecting maas without
    LLM_MAAS_API_KEY would 500 on first call."""
    cfg = cfg or _module_settings
    bt = (backend_type or "").lower()

    if bt == LLMBackendType.ollama.value:
        return bool(cfg.ollama_host)
    if bt == LLMBackendType.kserve.value:
        return bool(cfg.kserve_endpoint and cfg.kserve_model_name)
    if bt == LLMBackendType.vllm.value:
        return bool(cfg.vllm_endpoint and cfg.vllm_model_name)
    if bt == LLMBackendType.maas.value:
        return bool(cfg.llm_maas_base_url and cfg.llm_maas_model and cfg.llm_maas_api_key)
    if bt == LLMBackendType.mock.value:
        return True
    return False


def missing_config_for(backend_type: str, cfg: Settings | None = None) -> list[str]:
    """Return the list of missing env-var names for a backend type —
    used in the 422 response body when an operator picks an
    unconfigured backend so they know exactly what to set."""
    cfg = cfg or _module_settings
    bt = (backend_type or "").lower()
    missing: list[str] = []

    if bt == LLMBackendType.ollama.value:
        if not cfg.ollama_host:
            missing.append("OLLAMA_HOST")
    elif bt == LLMBackendType.kserve.value:
        if not cfg.kserve_endpoint:
            missing.append("KSERVE_ENDPOINT")
        if not cfg.kserve_model_name:
            missing.append("KSERVE_MODEL_NAME")
    elif bt == LLMBackendType.vllm.value:
        if not cfg.vllm_endpoint:
            missing.append("VLLM_ENDPOINT")
        if not cfg.vllm_model_name:
            missing.append("VLLM_MODEL_NAME")
    elif bt == LLMBackendType.maas.value:
        if not cfg.llm_maas_base_url:
            missing.append("LLM_MAAS_BASE_URL")
        if not cfg.llm_maas_model:
            missing.append("LLM_MAAS_MODEL")
        if not cfg.llm_maas_api_key:
            missing.append("LLM_MAAS_API_KEY")
    return missing


# ---------------------------------------------------------------------------
# Connection validation
# ---------------------------------------------------------------------------
@dataclass
class ConnectionTestResult:
    backend_type: str
    reachable: bool
    authenticated: bool
    model_available: bool
    latency_ms: int | None
    error: str | None

    def to_dict(self) -> dict:
        return {
            "backend_type": self.backend_type,
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "model_available": self.model_available,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


async def test_connection(backend_type: str, cfg: Settings | None = None) -> ConnectionTestResult:
    """Probe a backend's reachability + auth + model availability
    without mutating the active selection.

    Mock returns trivially ok. For real backends, this composes the
    backend's existing ``health_check()`` (which OpenAI-compatible
    backends implement as a ``/v1/models`` GET — cheap, no token
    spend) and translates the result into the operator-facing
    structure.
    """
    cfg = cfg or _module_settings
    bt = (backend_type or "").lower()

    if not is_backend_configured(bt, cfg):
        missing = missing_config_for(bt, cfg)
        return ConnectionTestResult(
            backend_type=bt,
            reachable=False,
            authenticated=False,
            model_available=False,
            latency_ms=None,
            error=(
                f"Backend '{bt}' is not configured: missing "
                f"{', '.join(missing) or 'connection config'}."
            ),
        )

    if bt == LLMBackendType.mock.value:
        return ConnectionTestResult(
            backend_type=bt,
            reachable=True,
            authenticated=True,
            model_available=True,
            latency_ms=0,
            error=None,
        )

    try:
        backend = get_llm_backend_for_type(bt, cfg)
    except LLMBackendError as exc:
        return ConnectionTestResult(
            backend_type=bt,
            reachable=False,
            authenticated=False,
            model_available=False,
            latency_ms=None,
            error=str(exc),
        )

    health = await backend.health_check()
    online = health.get("status") == "online"
    details = health.get("details") or {}
    auth_failed = bool(details.get("auth_failed"))
    # Reachable means the endpoint answered. A 401/403 back means it
    # answered, just rejected us — the operator should see "reachable
    # but auth failed" so they know to fix the key, not the URL.
    reachable = online or auth_failed
    model_loaded = details.get("model_loaded")
    if model_loaded is None:
        model_loaded = online  # backends without /models fall through to live-call

    error_detail = details.get("error")
    if online:
        error_text: str | None = None
    elif auth_failed:
        error_text = (
            f"{bt} authentication failed: the configured credentials "
            f"were rejected by the endpoint."
        )
    else:
        error_text = f"{bt} is unreachable" + (f": {error_detail}" if error_detail else ".")

    return ConnectionTestResult(
        backend_type=bt,
        reachable=reachable,
        authenticated=online and not auth_failed,
        model_available=bool(model_loaded) if online else False,
        latency_ms=health.get("latency_ms") if online else None,
        error=error_text,
    )
