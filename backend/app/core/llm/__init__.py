"""Pluggable LLM inference layer.

Public surface preserved from the pre-refactor ``app.core.llm`` module
so existing call sites (``from app.core.llm import LLMClient, LLMError``)
keep working unchanged. Backend selection happens in
:func:`app.core.llm.factory.get_llm_backend`; orchestrators that need
direct backend access import from there.
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
from app.core.llm.ollama_backend import OllamaBackend
from app.core.llm.vllm_backend import VLLMBackend

__all__ = [
    "LLMBackend",
    "LLMBackendError",
    "LLMClient",
    "LLMError",
    "OllamaBackend",
    "KServeBackend",
    "VLLMBackend",
    "SYSTEM_PROMPT",
    "get_llm_backend",
    "reset_backend_cache",
    "_diff_services",
    "_diff_ports",
    "_diff_mounts",
    "_diff_network",
    "_diff_cron",
]
