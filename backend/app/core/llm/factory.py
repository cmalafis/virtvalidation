"""Factory — picks the configured backend based on settings.

Every orchestrator (validation, planner, network review) calls into
``get_llm_backend()`` exactly once at construction time. The factory
is a single dispatch point so adding a new backend is a one-line
change here plus the new module.

A module-level cache avoids re-creating the backend on every request.
Reset it from tests via ``reset_backend_cache()`` when a fixture
mutates settings.
"""

from __future__ import annotations

import threading

from app.core.config import Settings, settings as _module_settings
from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.kserve_backend import KServeBackend
from app.core.llm.ollama_backend import OllamaBackend
from app.core.llm.vllm_backend import VLLMBackend

_SUPPORTED_BACKENDS = {"ollama", "kserve", "vllm"}

# Process-wide cache. The settings object is itself a singleton, so a
# single backend instance covers every request.
_lock = threading.Lock()
_cached: LLMBackend | None = None
_cached_for_type: str | None = None


def get_llm_backend(cfg: Settings | None = None) -> LLMBackend:
    """Return the configured backend, instantiating it on first call.

    Args:
        cfg: Override the imported ``settings`` singleton. Only tests
            should pass this — production code uses the default.
    """
    global _cached, _cached_for_type
    cfg = cfg or _module_settings
    backend_type = (cfg.llm_backend_type or "ollama").lower()

    if backend_type not in _SUPPORTED_BACKENDS:
        raise LLMBackendError(
            f"Unknown LLM_BACKEND_TYPE={backend_type!r}; "
            f"supported: {sorted(_SUPPORTED_BACKENDS)}"
        )

    with _lock:
        if _cached is not None and _cached_for_type == backend_type:
            return _cached
        backend = _instantiate(backend_type, cfg)
        _cached = backend
        _cached_for_type = backend_type
        return backend


def _instantiate(backend_type: str, cfg: Settings) -> LLMBackend:
    if backend_type == "ollama":
        return OllamaBackend(
            base_url=cfg.ollama_host,
            default_model=cfg.ollama_model,
        )
    if backend_type == "kserve":
        return KServeBackend(
            endpoint=cfg.kserve_endpoint or "",
            model_name=cfg.kserve_model_name or "",
            token=cfg.kserve_token,
            token_file=cfg.kserve_token_file,
            verify_ssl=cfg.kserve_verify_ssl,
            timeout=float(cfg.kserve_timeout_seconds),
        )
    if backend_type == "vllm":
        return VLLMBackend(
            endpoint=cfg.vllm_endpoint,
            model_name=cfg.vllm_model_name,
        )
    # _SUPPORTED_BACKENDS gate above already filters this — defensive only.
    raise LLMBackendError(f"Unhandled backend type: {backend_type}")  # pragma: no cover


def reset_backend_cache() -> None:
    """Drop the cached backend so the next ``get_llm_backend`` re-reads
    settings. Tests use this when they monkeypatch the config."""
    global _cached, _cached_for_type
    with _lock:
        _cached = None
        _cached_for_type = None
