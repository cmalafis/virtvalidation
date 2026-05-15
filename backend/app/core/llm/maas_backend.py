"""MaaS backend — authenticated Model-as-a-Service inference endpoints.

Talks to an OpenAI-compatible endpoint (LiteLLM proxy, OpenRouter,
self-hosted vLLM behind a reverse-proxy, etc.) that requires a static
bearer API key. Structurally a sibling of :class:`KServeBackend` —
same ``/v1/chat/completions`` request shape, same response parsing,
same exception mapping — but with two material differences:

  - The key is **required**, never optional. A request without
    Authorization defeats the point of the backend type.
  - The key is **static** (mounted from a Kubernetes Secret), not
    rotated from an SA-token file. No re-read on every call.

Secret hygiene rules (enforced by tests):

  - The key is never logged. ``__repr__`` redacts it.
  - The key is never embedded in raised exception messages — those
    surface the URL, status code, and reason only.
  - The Authorization header is built freshly per call inside
    ``_headers()`` and never logged. Anyone adding HTTP-level debug
    logging here MUST redact the header before emitting.
"""

from __future__ import annotations

import json
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


class MaaSBackend(LLMBackend):
    backend_type = "maas"

    # MaaS endpoints typically front Granite / Llama-class models with
    # 32K+ contexts and serve concurrent requests. Sized to match the
    # KServe defaults — granite-32-8b-instruct has comparable capacity
    # to the in-cluster KServe predictors. Tune via deployment if the
    # operator switches to a smaller-context model.
    max_planning_chunk_size = 50
    max_context_tokens = 32_000
    supports_concurrent_calls = True
    max_concurrent_calls = 3

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str,
        timeout: float = 60.0,
        verify_ssl: bool = True,
    ) -> None:
        if not base_url:
            raise LLMBackendError("MaaS backend requires LLM_MAAS_BASE_URL to be set")
        if not model_name:
            raise LLMBackendError("MaaS backend requires LLM_MAAS_MODEL to be set")
        if not api_key:
            raise LLMBackendError("MaaS backend requires LLM_MAAS_API_KEY to be set")

        self.endpoint = base_url.rstrip("/")
        self.default_model = model_name
        self._api_key = api_key
        self.timeout = float(timeout)
        self.verify_ssl = bool(verify_ssl)

    def __repr__(self) -> str:
        # The key is never reproduced — even in logs that accidentally
        # repr() the backend instance. ``bearer:****`` advertises auth
        # is configured without saying what.
        return (
            f"<MaaSBackend endpoint={self.endpoint!r} "
            f"model={self.default_model!r} auth=bearer:****>"
        )

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        # Built per-call. Don't cache the dict on self — that creates a
        # second place a future debugger might dump the key from.
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

    # -----------------------------------------------------------------
    # OpenAI-compatible chat completions
    # -----------------------------------------------------------------
    def _chat_url(self) -> str:
        return f"{self.endpoint}/chat/completions"

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        payload: dict = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_ssl) as client:
                resp = await client.post(self._chat_url(), json=payload, headers=self._headers())
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status_code = e.response.status_code
            if status_code in (401, 403):
                # Operator config error — wrong/expired key. Surfaced
                # specifically by the planner's annotation orchestrator
                # so the Settings UI can show a banner. Note: ``e`` is
                # NOT chained into the message because some httpx error
                # reprs include request headers — we only want the URL
                # and status to escape.
                raise LLMAuthError(
                    f"MaaS refused auth (HTTP {status_code}) at "
                    f"{self.endpoint}. Check the configured API key."
                ) from None
            if status_code == 429:
                # Rate limit — distinct from generic 5xx so the
                # planner can decide whether to back off.
                raise LLMResponseError(
                    f"MaaS rate-limited (HTTP 429) at {self.endpoint}."
                ) from None
            raise LLMResponseError(
                f"MaaS returned HTTP {status_code} at {self.endpoint}."
            ) from None
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(
                f"MaaS request to {self.endpoint} timed out " f"({self.timeout}s budget): {e}"
            ) from e
        except httpx.TransportError as e:
            raise LLMUnreachableError(f"Cannot reach MaaS at {self.endpoint}: {e}") from e
        except httpx.HTTPError as e:
            raise LLMBackendError(f"MaaS request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise LLMResponseError(f"MaaS returned non-JSON envelope: {e}") from e

        choices = body.get("choices") or []
        if not choices:
            raise LLMResponseError("MaaS returned no choices in response")
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if not content:
            raise LLMResponseError("MaaS returned an empty message")

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
        payload = {
            "model": model or self.default_model,
            "messages": messages,
            "temperature": temperature,
            "stream": True,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout, verify=self.verify_ssl) as client:
                async with client.stream(
                    "POST",
                    self._chat_url(),
                    json=payload,
                    headers=self._headers(),
                ) as resp:
                    if resp.status_code in (401, 403):
                        raise LLMAuthError(
                            f"MaaS refused auth (HTTP {resp.status_code}) at "
                            f"{self.endpoint}. Check the configured API key."
                        )
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        body = line[len("data:") :].strip()
                        if body == "[DONE]":
                            return
                        try:
                            chunk = json.loads(body)
                        except ValueError:
                            continue
                        delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                        token = delta.get("content")
                        if token:
                            yield token
        except httpx.HTTPError as e:
            raise LLMBackendError(f"MaaS stream failed: {e}") from e

    async def health_check(self) -> dict:
        started = time.monotonic()
        url = f"{self.endpoint}/models"
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=self.verify_ssl) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else -1
            return {
                "status": "offline",
                "backend": self.backend_type,
                "model": self.default_model,
                "endpoint": self.endpoint,
                "latency_ms": -1,
                "details": {
                    "error": f"HTTP {status}",
                    "auth_failed": status in (401, 403),
                },
            }
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
            m.get("id") for m in (body.get("data") or []) if isinstance(m, dict) and m.get("id")
        ]
        return {
            "status": "online",
            "backend": self.backend_type,
            "model": self.default_model,
            "endpoint": self.endpoint,
            "latency_ms": latency_ms,
            "details": {
                "available_models": available,
                "model_loaded": (self.default_model in available) if available else None,
                "auth": "bearer:****",
            },
        }

    def list_models(self) -> list[str]:
        try:
            with httpx.Client(timeout=5.0, verify=self.verify_ssl) as client:
                resp = client.get(f"{self.endpoint}/models", headers=self._headers())
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError):
            return [self.default_model]
        names = [
            m.get("id") for m in (body.get("data") or []) if isinstance(m, dict) and m.get("id")
        ]
        return names or [self.default_model]
