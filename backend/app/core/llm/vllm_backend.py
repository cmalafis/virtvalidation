"""vLLM backend — placeholder for v1.0.0.

Direct vLLM connection without the KServe wrapper. The v1.0.0
target is a high-scale concurrent-inference path tuned for fleets
running validation against hundreds of VMs in parallel.

Until that lands, every method raises ``NotImplementedError`` so a
deployment that misconfigures ``LLM_BACKEND_TYPE=vllm`` fails loud
at the first request rather than silently failing later.

The class still exists so the factory's dispatch table is complete
and so config validation accepts ``vllm`` as a legal value.
"""

from __future__ import annotations

from typing import AsyncIterator

from app.core.llm.base import LLMBackend


_NOT_READY_MESSAGE = (
    "vLLM backend is not implemented yet — targeted for VirtValidate v1.0.0. "
    "Until then, use LLM_BACKEND_TYPE=ollama (standalone) or "
    "LLM_BACKEND_TYPE=kserve (RHOAI / OpenShift)."
)


class VLLMBackend(LLMBackend):
    backend_type = "vllm"

    # Direct vLLM connection — large context (Llama 3.1 128K), in-flight
    # batching means high concurrent throughput. Conservatively cap at
    # 5 concurrent calls because the planner's per-chunk waves are
    # already coarse parallelism and we don't want to thrash a shared
    # GPU. Bump higher in vllm_backend config if you have dedicated
    # capacity.
    max_planning_chunk_size = 75
    max_context_tokens = 128_000
    supports_concurrent_calls = True
    max_concurrent_calls = 5

    def __init__(
        self,
        endpoint: str | None = None,
        model_name: str | None = None,
    ) -> None:
        # Stash the config so the health probe can return something
        # useful instead of an exception even though chat/stream don't
        # work yet.
        self.endpoint = (endpoint or "").rstrip("/") or None
        self.default_model = model_name

    async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
        raise NotImplementedError(_NOT_READY_MESSAGE)

    async def chat_stream(self, messages, model=None, temperature=0.1) -> AsyncIterator[str]:
        # Generators must yield once before raising for the AsyncIterator
        # type to be honored. Raise unconditionally instead.
        raise NotImplementedError(_NOT_READY_MESSAGE)
        if False:  # pragma: no cover  — keeps mypy happy as an async iterator
            yield ""

    async def health_check(self) -> dict:
        return {
            "status": "offline",
            "backend": self.backend_type,
            "model": self.default_model,
            "endpoint": self.endpoint,
            "latency_ms": -1,
            "details": {
                "error": _NOT_READY_MESSAGE,
                "planned_release": "v1.0.0",
            },
        }

    def list_models(self) -> list[str]:
        return [self.default_model] if self.default_model else []
