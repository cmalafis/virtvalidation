"""Pluggable LLM inference layer.

Two resolvers here, with different jobs:

  - :func:`app.core.llm.factory.get_llm_backend` — env-var driven
    bootstrap path. Used by the migration's seed step and tests that
    pin a specific backend via ``Settings(llm_backend_type=...)``.
  - :func:`app.core.llm.runtime.get_active_backend` — DB-backed
    runtime resolver. The current operator-selected backend, with a
    short TTL cache. Production orchestrators (planner, categorizer,
    validation client) call this so an operator can flip the active
    backend without a redeploy.
"""

from app.core.llm.base import LLMBackend, LLMBackendError
from app.core.llm.client import (
    SYSTEM_PROMPT,
    LLMClient,
    LLMError,
    _diff_cron,
    _diff_mounts,
    _diff_network,
    _diff_ports,
    _diff_services,
)
from app.core.llm.factory import get_llm_backend, reset_backend_cache
from app.core.llm.kserve_backend import KServeBackend
from app.core.llm.maas_backend import MaaSBackend
from app.core.llm.ollama_backend import OllamaBackend
from app.core.llm.runtime import (
    ConnectionTestResult,
    get_active_backend,
    test_connection,
)
from app.core.llm.runtime import (
    invalidate as invalidate_active_backend_cache,
)
from app.core.llm.types import LLMBackendType
from app.core.llm.vllm_backend import VLLMBackend

__all__ = [
    "ConnectionTestResult",
    "KServeBackend",
    "LLMBackend",
    "LLMBackendError",
    "LLMBackendType",
    "LLMClient",
    "LLMError",
    "MaaSBackend",
    "OllamaBackend",
    "SYSTEM_PROMPT",
    "VLLMBackend",
    "_diff_cron",
    "_diff_mounts",
    "_diff_network",
    "_diff_ports",
    "_diff_services",
    "get_active_backend",
    "get_llm_backend",
    "invalidate_active_backend_cache",
    "reset_backend_cache",
    "test_connection",
]
