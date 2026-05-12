"""Ollama backend — default for standalone, air-gapped deployments.

Talks to the Ollama HTTP API at ``$OLLAMA_HOST``. Wraps the existing
single-instance Ollama deployment that ships with podman-compose.

Three behaviors here that aren't in the abstract base class because
they're transport-specific:

  - ``num_ctx`` is plumbed through ``options`` so prompts longer than
    Ollama's default 4096 don't silently truncate.
  - The httpx timeout gives reads 600s by default — CPU inference of
    Llama 3 8B on a single host routinely takes minutes for batched
    prompts, and the original 120s ceiling was racing the model.
  - Transient transport failures (timeouts, connection resets) get
    retried with backoff — 5s, then 30s, then give up. 4xx/5xx HTTP
    errors propagate immediately because they're deterministic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

import httpx

from app.core.llm.base import (
    LLMAuthError,
    LLMBackend,
    LLMBackendError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnreachableError,
)

logger = logging.getLogger(__name__)


# Backoff schedule for transport-level retries. The first failure waits
# 5s (typically enough for Ollama to finish loading a cold model), the
# second waits 30s (enough for a model swap on small hosts).
_RETRY_BACKOFF_SECONDS = (5.0, 30.0)


def _approx_token_count(messages: list[dict]) -> int:
    """Char-count / 4 token estimate. Good enough for guardrail
    decisions; we don't ship a tiktoken-style tokenizer with the
    appliance because Ollama doesn't expose one."""
    chars = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            chars += len(content)
    return chars // 4


class OllamaBackend(LLMBackend):
    backend_type = "ollama"

    # Llama 3 8B on a single Ollama host. The 15-VM chunk fits the 8192
    # context with ~2K tokens of headroom for the JSON wave output.
    # Single-stream — Ollama serializes /api/chat across clients, so
    # concurrent calls just queue.
    max_planning_chunk_size = 15
    max_context_tokens = 8192
    supports_concurrent_calls = False
    max_concurrent_calls = 1

    def __init__(
        self,
        base_url: str = "http://ollama:11434",
        default_model: str = "llama3:8b",
        timeout: float | None = None,
        connect_timeout: float = 30.0,
        num_ctx: int = 8192,
        max_retries: int = 2,
    ) -> None:
        self.endpoint = base_url.rstrip("/")
        self.default_model = default_model
        # Backwards-compatible: legacy callers pass a single ``timeout``
        # float, which we apply to the read leg only. The connect leg
        # always stays at 30s — pointless to wait longer on TCP setup.
        self.read_timeout = float(timeout) if timeout is not None else 600.0
        self.connect_timeout = float(connect_timeout)
        self.num_ctx = int(num_ctx)
        self.max_retries = max(0, int(max_retries))
        # Cached for older code paths that grep this attribute.
        self.timeout = self.read_timeout

    def _httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=self.connect_timeout,
            read=self.read_timeout,
            write=self.connect_timeout,
            pool=self.connect_timeout,
        )

    def _maybe_warn_prompt_size(self, messages: list[dict]) -> None:
        """Log a structured warning when the prompt is close to the
        configured context window. We don't refuse the call — the caller
        should still see the response Ollama produces (or the truncation
        warning Ollama itself logs) — but the operator gets advance
        warning before output quality degrades."""
        approx_tokens = _approx_token_count(messages)
        threshold = int(self.num_ctx * 0.8)
        if approx_tokens > threshold:
            logger.warning(
                "Ollama prompt approaches context window: approx_tokens=%d "
                "num_ctx=%d threshold=%d. Output may be truncated; reduce "
                "batch size or increase OLLAMA_NUM_CTX.",
                approx_tokens,
                self.num_ctx,
                threshold,
            )

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        self._maybe_warn_prompt_size(messages)

        payload: dict = {
            "model": model or self.default_model,
            "stream": False,
            # Ollama's structured-output mode — guarantees valid JSON
            # comes back when the orchestrator asked for JSON.
            "format": "json",
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": temperature,
            },
            "messages": messages,
        }
        if max_tokens is not None:
            # Ollama uses num_predict, not max_tokens.
            payload["options"]["num_predict"] = max_tokens

        # Retry only on transport-level failures. HTTP status errors
        # (raise_for_status) are deterministic — retrying a 400 won't
        # change the answer.
        last_err: Exception | None = None
        attempt_count = self.max_retries + 1
        for attempt in range(attempt_count):
            if attempt > 0:
                # The retry index is 1-based for the schedule lookup.
                wait = _RETRY_BACKOFF_SECONDS[min(attempt - 1, len(_RETRY_BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "Ollama transport failure on attempt %d/%d (%s); " "retrying in %.1fs",
                    attempt,
                    attempt_count,
                    last_err,
                    wait,
                )
                await asyncio.sleep(wait)
            try:
                async with httpx.AsyncClient(timeout=self._httpx_timeout()) as client:
                    resp = await client.post(f"{self.endpoint}/api/chat", json=payload)
                    resp.raise_for_status()
                break
            except httpx.HTTPStatusError as e:
                # 4xx/5xx — surface immediately, no retry. Auth gets
                # its own type so the planner can surface the
                # KServe / Ollama-with-API-key story specifically.
                status_code = e.response.status_code
                if status_code in (401, 403):
                    raise LLMAuthError(f"Ollama refused auth (HTTP {status_code}): {e}") from e
                raise LLMResponseError(f"Ollama returned HTTP {status_code}: {e}") from e
            except httpx.TimeoutException as e:
                last_err = e
                if attempt == attempt_count - 1:
                    raise LLMTimeoutError(
                        f"Ollama request timed out after {attempt_count} "
                        f"attempts ({self.read_timeout}s read budget): {e}"
                    ) from e
            except httpx.TransportError as e:
                # Network-layer failure — DNS, connection refused,
                # TLS handshake — distinguish from HTTP errors so
                # the operator sees "connectivity" vs "server-side"
                # in the plan error_message.
                last_err = e
                if attempt == attempt_count - 1:
                    raise LLMUnreachableError(
                        f"Cannot reach Ollama at {self.endpoint} after "
                        f"{attempt_count} attempts: {e}"
                    ) from e
            except httpx.HTTPError as e:
                # Anything else — give up immediately.
                raise LLMBackendError(f"Ollama request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise LLMResponseError(f"Ollama returned non-JSON envelope: {e}") from e

        message = body.get("message") or {}
        content = message.get("content", "")
        if not content:
            raise LLMResponseError("Ollama returned an empty message")

        return {
            "content": content,
            "model": body.get("model", payload["model"]),
        }

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
    ) -> AsyncIterator[str]:
        self._maybe_warn_prompt_size(messages)
        payload = {
            "model": model or self.default_model,
            "stream": True,
            "options": {
                "num_ctx": self.num_ctx,
                "temperature": temperature,
            },
            "messages": messages,
        }
        try:
            async with httpx.AsyncClient(timeout=self._httpx_timeout()) as client:
                # Ollama emits one JSON object per line; iter_lines is the
                # right primitive here, not iter_text.
                async with client.stream("POST", f"{self.endpoint}/api/chat", json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except ValueError:
                            continue
                        msg = chunk.get("message") or {}
                        token = msg.get("content")
                        if token:
                            yield token
                        if chunk.get("done"):
                            return
        except httpx.HTTPError as e:
            raise LLMBackendError(f"Ollama stream failed: {e}") from e

    async def health_check(self) -> dict:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.endpoint}/api/tags")
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            return {
                "status": "offline",
                "backend": self.backend_type,
                "model": self.default_model,
                "endpoint": self.endpoint,
                "latency_ms": -1,
                "details": {"error": str(e)},
            }
        latency_ms = int((time.monotonic() - started) * 1000)
        available = [
            m["name"] for m in (body.get("models") or []) if isinstance(m, dict) and m.get("name")
        ]
        return {
            "status": "online",
            "backend": self.backend_type,
            "model": self.default_model,
            "endpoint": self.endpoint,
            "latency_ms": latency_ms,
            "details": {
                "available_models": available,
                "model_loaded": self.default_model in available,
                "num_ctx": self.num_ctx,
                "read_timeout_s": self.read_timeout,
            },
        }

    def list_models(self) -> list[str]:
        # Sync probe of /api/tags so the system endpoint can call this
        # from non-async code paths. Failures bubble up as an empty list
        # so the UI degrades gracefully.
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(f"{self.endpoint}/api/tags")
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        return [
            m["name"] for m in (body.get("models") or []) if isinstance(m, dict) and m.get("name")
        ]
