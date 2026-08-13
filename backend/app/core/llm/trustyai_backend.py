"""TrustyAI Guardrails Orchestrator backend (RHOAI).

Routes inference through the in-cluster TrustyAI Guardrails Orchestrator
instead of talking to a model directly. The orchestrator runs configured
input/output detectors (prompt-injection, hap, …) in front of an
OpenAI-compatible model and returns both the model's answer AND any detector
hits.

Wire protocol (confirmed against the RHOAI / FMS-Guardrails docs):

  POST {base}/api/v2/chat/completions-detection
  {
    "model": "...",
    "messages": [...],
    "detectors": {"input": {"prompt_injection": {}}, "output": {...}}
  }

  → 200 OpenAI-style envelope. On a clean call the answer is at
    ``choices[0].message.content`` (so the existing verdict/annotation
    parsers work unchanged). When a detector fires the body carries a
    ``warnings`` array (``UNSUITABLE_INPUT`` / ``UNSUITABLE_OUTPUT``) plus a
    structured ``detections`` object — we raise :class:`LLMGuardrailError`,
    which the orchestrators treat asymmetrically (mechanical fallback + a
    Settings-UI banner), distinct from a quiet transient failure.

Secret hygiene mirrors the MaaS backend: the API key (optional here — an
in-cluster orchestrator may be unauthenticated) is never logged, never in an
exception message, and ``__repr__`` redacts it.
"""

from __future__ import annotations

import time
from typing import AsyncIterator

import httpx

from app.core.llm.base import (
    LLMAuthError,
    LLMBackend,
    LLMBackendError,
    LLMGuardrailError,
    LLMResponseError,
    LLMTimeoutError,
    LLMUnreachableError,
)


def _parse_detectors(spec: str | None) -> dict[str, dict]:
    """``"prompt_injection, hap"`` → ``{"prompt_injection": {}, "hap": {}}``."""
    if not spec:
        return {}
    return {name.strip(): {} for name in spec.split(",") if name.strip()}


class TrustyAIBackend(LLMBackend):
    backend_type = "trustyai"

    # The orchestrator fronts a Granite/Llama-class model with detectors; size
    # to the MaaS/KServe envelope. The detector round-trip adds latency but no
    # context change.
    max_planning_chunk_size = 50
    max_context_tokens = 32_000
    supports_concurrent_calls = True
    max_concurrent_calls = 3

    def __init__(
        self,
        base_url: str,
        model_name: str,
        api_key: str | None = None,
        input_detectors: str | None = "prompt_injection",
        output_detectors: str | None = "",
        timeout: float = 60.0,
        verify_ssl: bool = True,
    ) -> None:
        if not base_url:
            raise LLMBackendError("TrustyAI backend requires LLM_TRUSTYAI_BASE_URL to be set")
        if not model_name:
            raise LLMBackendError("TrustyAI backend requires LLM_TRUSTYAI_MODEL to be set")

        self.endpoint = base_url.rstrip("/")
        self.default_model = model_name
        self._api_key = api_key or ""
        self._input_detectors = _parse_detectors(input_detectors)
        self._output_detectors = _parse_detectors(output_detectors)
        self.timeout = float(timeout)
        self.verify_ssl = bool(verify_ssl)

    def __repr__(self) -> str:
        auth = "bearer:****" if self._api_key else "none"
        return (
            f"<TrustyAIBackend endpoint={self.endpoint!r} "
            f"model={self.default_model!r} auth={auth} "
            f"input_detectors={sorted(self._input_detectors)} "
            f"output_detectors={sorted(self._output_detectors)}>"
        )

    # -----------------------------------------------------------------
    # Auth — optional bearer. Built per-call, never cached/logged.
    # -----------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _chat_url(self) -> str:
        return f"{self.endpoint}/api/v2/chat/completions-detection"

    def _detectors_block(self) -> dict:
        block: dict[str, dict] = {}
        if self._input_detectors:
            block["input"] = self._input_detectors
        if self._output_detectors:
            block["output"] = self._output_detectors
        return block

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
            "detectors": self._detectors_block(),
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
                raise LLMAuthError(
                    f"TrustyAI orchestrator refused auth (HTTP {status_code}) at "
                    f"{self.endpoint}. Check the configured API key."
                ) from None
            if status_code == 429:
                raise LLMResponseError(
                    f"TrustyAI orchestrator rate-limited (HTTP 429) at {self.endpoint}."
                ) from None
            raise LLMResponseError(
                f"TrustyAI orchestrator returned HTTP {status_code} at {self.endpoint}."
            ) from None
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(
                f"TrustyAI request to {self.endpoint} timed out ({self.timeout}s budget): {e}"
            ) from e
        except httpx.TransportError as e:
            raise LLMUnreachableError(
                f"Cannot reach TrustyAI orchestrator at {self.endpoint}: {e}"
            ) from e
        except httpx.HTTPError as e:
            raise LLMBackendError(f"TrustyAI request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise LLMResponseError(f"TrustyAI returned non-JSON envelope: {e}") from e

        # Guardrail hit — a 200 with a ``warnings`` array. Raise the asymmetric
        # error carrying the structured detections for the audit log. We do NOT
        # echo the flagged text into the message (it may be the very thing a
        # detector objected to); only the warning types escape.
        warnings = body.get("warnings") or []
        if warnings:
            types = ", ".join(str(w.get("type", "UNKNOWN")) for w in warnings)
            raise LLMGuardrailError(
                f"TrustyAI guardrail detector flagged content ({types}).",
                detections=body.get("detections") or {},
            )

        choices = body.get("choices") or []
        if not choices:
            raise LLMResponseError("TrustyAI returned no choices in response")
        content = (choices[0].get("message") or {}).get("content", "")
        if not content:
            raise LLMResponseError("TrustyAI returned an empty message")
        return {"content": content, "model": body.get("model", payload["model"])}

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
    ) -> AsyncIterator[str]:
        # The detection endpoint needs the full text to run output detectors,
        # so streaming isn't meaningful here — yield the whole completion as a
        # single chunk (permitted by the base contract). A guardrail hit
        # propagates as LLMGuardrailError from chat().
        result = await self.chat(messages=messages, model=model, temperature=temperature)
        yield result["content"]

    async def health_check(self) -> dict:
        started = time.monotonic()
        url = f"{self.endpoint}/health"
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=self.verify_ssl) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code if e.response is not None else -1
            return {
                "status": "offline",
                "backend": self.backend_type,
                "model": self.default_model,
                "endpoint": self.endpoint,
                "latency_ms": -1,
                "details": {"error": f"HTTP {status}", "auth_failed": status in (401, 403)},
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
        return {
            "status": "online",
            "backend": self.backend_type,
            "model": self.default_model,
            "endpoint": self.endpoint,
            "latency_ms": latency_ms,
            "details": {
                "input_detectors": sorted(self._input_detectors),
                "output_detectors": sorted(self._output_detectors),
                "auth": "bearer:****" if self._api_key else "none",
            },
        }

    def list_models(self) -> list[str]:
        # The orchestrator fronts the single configured model.
        return [self.default_model]
