"""Pluggable LLM inference backend.

Every backend (Ollama, KServe, vLLM) implements the same abstract
contract so the orchestration layer (validation, planner, network
review) can swap inference engines via configuration alone.

The interface is intentionally small — chat, chat_stream, health
check, list models. High-level domain logic (prompt templates,
verdict parsing, diff engines) lives in the orchestrators above
this layer; backends are pure transport.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import AsyncIterator


class LLMBackendError(RuntimeError):
    """Raised when a backend call fails — transport, parsing, or auth.

    Concrete subclasses below let the planner write a specific
    ``Plan.error_message`` instead of a generic "LLM failed" — which
    is what federal operators need when a plan generation fails: was
    it a wrong URL, missing token, timeout, or model error?
    """


class LLMUnreachableError(LLMBackendError):
    """Connection refused, DNS failure, network partition.

    Likely cause: ``LLM_BASE_URL`` env var is empty / wrong, or the
    backend pod can't egress to the inference endpoint. The startup
    health-check log shows this on every pod restart.
    """


class LLMAuthError(LLMBackendError):
    """Authentication failed — 401 / 403 from the inference endpoint.

    KServe ``InferenceService`` with auth enabled needs the pod's
    service account token (or an operator-supplied bearer token).
    Resolution: check ``kserve_token_file`` mount + the SA's
    RoleBinding on the inference namespace.
    """


class LLMTimeoutError(LLMBackendError):
    """The inference call exceeded its read timeout.

    Default 600s read timeout on Ollama; KServe is bounded by
    ``kserve_timeout_seconds`` (default 120s). Llama 3 8B CPU-only
    inference can exceed both on cold-model first calls.
    """


class LLMResponseError(LLMBackendError):
    """Endpoint returned a non-2xx status or malformed body.

    Distinct from ``LLMUnreachableError`` (transport OK, model said
    no) and ``LLMResponseError`` from a 200 with garbage JSON. The
    response detail is surfaced verbatim in ``Plan.error_message``
    so the operator sees what the model actually returned.
    """


class LLMBackend(ABC):
    """Abstract base for all LLM inference backends.

    Concrete subclasses must implement the four methods below. They
    are async because every transport we care about (Ollama, KServe,
    vLLM) has an HTTP API that benefits from non-blocking I/O. The
    sync orchestration code calls into ``chat_sync`` (provided here)
    which bridges via ``asyncio.run`` so existing FastAPI BackgroundTask
    code paths keep working without becoming async.

    Each backend instance is responsible for its own connection state
    (HTTP client, auth token caching). Instances are cheap to create —
    one per request is fine; one shared at app startup is also fine.
    """

    #: Human-readable identifier surfaced to the UI / health endpoints.
    backend_type: str = "abstract"

    # ---- Capacity declarations consumed by the hierarchical planner ----
    # These are class-level defaults; concrete backends override them
    # with values that match the model + serving environment they
    # represent. The planner reads them at runtime so the same code
    # scales from a 4096-context Ollama appliance to a multi-pod KServe
    # deployment without orchestrator changes.
    #
    # max_planning_chunk_size — VMs per per-chunk LLM call. Sized so the
    #   chunk's prompt + JSON output stays comfortably inside
    #   ``max_context_tokens``.
    # max_context_tokens — model's effective context window in tokens.
    # supports_concurrent_calls — True when the backend can serve >1
    #   chat call concurrently (KServe with >1 replica, vLLM batching).
    # max_concurrent_calls — upper bound on parallelism the planner
    #   uses for chunks. Ignored when supports_concurrent_calls=False.
    max_planning_chunk_size: int = 15
    max_context_tokens: int = 4096
    supports_concurrent_calls: bool = False
    max_concurrent_calls: int = 1

    @abstractmethod
    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        """Single completion request, returns full response.

        Args:
            messages: OpenAI-compatible message list — each entry has
                ``role`` and ``content``. The first message is typically
                the system prompt.
            model: Override the backend's default model. ``None`` means
                use the model configured at construction time.
            temperature: Sampling temperature. 0.0 is deterministic.
            max_tokens: Optional cap on the response token count.

        Returns:
            ``{"content": str, "model": str}`` at minimum. Backends MAY
            include extra fields (``usage``, ``finish_reason``) — callers
            must not rely on their presence.
        """

    @abstractmethod
    def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
    ) -> AsyncIterator[str]:
        """Streaming completion — yields token chunks as they arrive.

        Backends that don't support streaming may yield the full
        completion as a single chunk. Implementations must be async
        generators (``async def`` + ``yield``).
        """

    @abstractmethod
    async def health_check(self) -> dict:
        """Verify backend is reachable and the model is loaded.

        Returns:
            Dict with the keys:
              - ``status``     — "online" | "offline"
              - ``backend``    — backend_type
              - ``model``      — configured default model
              - ``endpoint``   — URL the backend is talking to
              - ``latency_ms`` — round-trip on the probe call (-1 if offline)
              - ``details``    — backend-specific extras (available models,
                                 error message, etc.)
        """

    @abstractmethod
    def list_models(self) -> list[str]:
        """List available models on this backend.

        For backends that serve a single model (KServe), this returns a
        one-element list. For multi-model backends (Ollama), it returns
        every model currently loaded.
        """

    # -----------------------------------------------------------------
    # Sync bridge — non-abstract helper so callers in BackgroundTask
    # threads can use the async chat() without becoming async themselves.
    # -----------------------------------------------------------------
    def chat_sync(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        """Synchronous wrapper around :meth:`chat`.

        BackgroundTasks run in the event loop's thread pool with no
        running loop on the worker thread, so ``asyncio.run`` is safe.
        Tests that already drive an event loop should call ``chat``
        directly to avoid nesting.
        """
        return asyncio.run(
            self.chat(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        )

    def health_check_sync(self) -> dict:
        """Synchronous wrapper around :meth:`health_check`."""
        return asyncio.run(self.health_check())

    def info(self) -> dict:
        """Static configuration snapshot — what backend is wired up.

        Used by the Settings UI / system endpoints to show the operator
        what they're talking to without making a network call. The
        ``health_check`` result complements this with live status.
        """
        return {
            "backend": self.backend_type,
            "model": getattr(self, "default_model", None),
            "endpoint": getattr(self, "endpoint", None),
        }
