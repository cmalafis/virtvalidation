"""Factory — picks the configured backend based on settings.

Every orchestrator (validation, planner, network review) calls into
``get_llm_backend()`` exactly once at construction time. The factory
is a single dispatch point so adding a new backend is a one-line
change here plus the new module.

A module-level cache avoids re-creating the backend on every request.
The cache is **keyed by backend_type** so the runtime resolver
(``app.core.llm.runtime``) can flip the active backend without
churning instantiation — switching from ``ollama`` to ``maas`` and
back keeps each instance hot.

Reset it from tests via ``reset_backend_cache()`` when a fixture
mutates settings.
"""

from __future__ import annotations

import threading

from app.core.config import Settings
from app.core.config import settings as _module_settings
from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.kserve_backend import KServeBackend
from app.core.llm.maas_backend import MaaSBackend
from app.core.llm.mock_backend import MockBackend
from app.core.llm.ollama_backend import OllamaBackend
from app.core.llm.trustyai_backend import TrustyAIBackend
from app.core.llm.types import LLMBackendType
from app.core.llm.vllm_backend import VLLMBackend

_SUPPORTED_BACKENDS = {m.value for m in LLMBackendType}

# Process-wide cache, keyed by backend_type. Multiple types can be
# warm simultaneously so the runtime resolver can switch without a
# fresh httpx client setup on every flip.
_lock = threading.Lock()
_cached: dict[str, LLMBackend] = {}


def get_llm_backend(cfg: Settings | None = None) -> LLMBackend:
    """Return the backend identified by ``cfg.llm_backend_type``.

    This is the env-var-driven resolver — the bootstrap path used by
    the migration's seed step and by tests that explicitly set
    ``llm_backend_type``. Production runtime code should call
    :func:`app.core.llm.runtime.get_active_backend` instead, which
    reads the DB-backed setting.

    Args:
        cfg: Override the imported ``settings`` singleton. Only tests
            should pass this — production code uses the default.
    """
    cfg = cfg or _module_settings
    backend_type = (cfg.llm_backend_type or "ollama").lower()
    return get_llm_backend_for_type(backend_type, cfg)


def get_llm_backend_for_type(backend_type: str, cfg: Settings | None = None) -> LLMBackend:
    """Return the backend instance for an explicit type, instantiating
    on first call. Cached per-type so the runtime resolver can switch
    backends without rebuilding clients.
    """
    cfg = cfg or _module_settings
    backend_type = (backend_type or "").lower()

    if backend_type not in _SUPPORTED_BACKENDS:
        raise LLMBackendError(
            f"Unknown LLM_BACKEND_TYPE={backend_type!r}; "
            f"supported: {sorted(_SUPPORTED_BACKENDS)}"
        )

    with _lock:
        cached = _cached.get(backend_type)
        if cached is not None:
            return cached
        backend = _instantiate(backend_type, cfg)
        _cached[backend_type] = backend
        return backend


def _instantiate(backend_type: str, cfg: Settings) -> LLMBackend:
    if backend_type == LLMBackendType.ollama.value:
        return OllamaBackend(
            base_url=cfg.ollama_host,
            default_model=cfg.ollama_model,
            timeout=cfg.llm_read_timeout,
            connect_timeout=cfg.llm_connect_timeout,
            num_ctx=cfg.ollama_num_ctx,
            max_retries=cfg.llm_max_retries,
        )
    if backend_type == LLMBackendType.kserve.value:
        return KServeBackend(
            endpoint=cfg.kserve_endpoint or "",
            model_name=cfg.kserve_model_name or "",
            token=cfg.kserve_token,
            token_file=cfg.kserve_token_file,
            verify_ssl=cfg.kserve_verify_ssl,
            timeout=float(cfg.kserve_timeout_seconds),
        )
    if backend_type == LLMBackendType.vllm.value:
        return VLLMBackend(
            endpoint=cfg.vllm_endpoint,
            model_name=cfg.vllm_model_name,
        )
    if backend_type == LLMBackendType.maas.value:
        return MaaSBackend(
            base_url=cfg.llm_maas_base_url or "",
            model_name=cfg.llm_maas_model or "",
            api_key=cfg.llm_maas_api_key or "",
            timeout=float(cfg.llm_maas_timeout_seconds),
            verify_ssl=cfg.llm_maas_verify_ssl,
        )
    if backend_type == LLMBackendType.trustyai.value:
        return TrustyAIBackend(
            base_url=cfg.llm_trustyai_base_url or "",
            model_name=cfg.llm_trustyai_model or "",
            api_key=cfg.llm_trustyai_api_key,
            input_detectors=cfg.llm_trustyai_input_detectors,
            output_detectors=cfg.llm_trustyai_output_detectors,
            timeout=float(cfg.llm_trustyai_timeout_seconds),
            verify_ssl=cfg.llm_trustyai_verify_ssl,
        )
    if backend_type == LLMBackendType.mock.value:
        # Dev-only canned-response backend. See app/core/llm/mock_backend.py
        # and docs/MOCK_BACKEND.md — never deploy this to production.
        return MockBackend()
    # _SUPPORTED_BACKENDS gate above already filters this — defensive only.
    raise LLMBackendError(f"Unhandled backend type: {backend_type}")  # pragma: no cover


def reset_backend_cache(backend_type: str | None = None) -> None:
    """Drop cached backend(s) so the next call re-instantiates from
    settings.

    Pass a specific ``backend_type`` to drop just one (used by the
    runtime resolver when config for that type changes); pass nothing
    to drop everything (used by tests that monkeypatch the config).
    """
    with _lock:
        if backend_type is None:
            _cached.clear()
        else:
            _cached.pop(backend_type.lower(), None)
