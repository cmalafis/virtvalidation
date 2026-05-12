"""KServe backend — RHOAI / OpenShift inference deployments.

Talks to a KServe InferenceService that exposes the OpenAI-compatible
``/v1/chat/completions`` API. vLLM and TGIS predictors both expose
this surface; selecting one is a deployment decision the operator
makes when standing up the model — VirtValidate doesn't care.

Authentication:

  - In-cluster: VirtValidate runs as a pod with a service account
    whose token is mounted at ``/var/run/secrets/kubernetes.io/serviceaccount/token``.
    The token file is read on every request so rotations apply
    without a restart.
  - Out-of-cluster: the operator must set ``KSERVE_TOKEN`` explicitly.
  - Open / unauthenticated: leave both unset; the request goes out
    without an Authorization header (only meaningful for dev clusters
    that disabled token auth).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
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


class KServeBackend(LLMBackend):
    backend_type = "kserve"

    # Larger model + horizontal scaling on RHOAI. KServe predictors
    # typically front a 32K-context model (Llama 3.1 8B/70B, Mistral
    # Large) and serve concurrent requests across replicas. The
    # 50-VM chunk + 3-way parallelism is the sweet spot validated on
    # the RHOAI demo cluster's 3-replica config.
    max_planning_chunk_size = 50
    max_context_tokens = 32_000
    supports_concurrent_calls = True
    max_concurrent_calls = 3

    def __init__(
        self,
        endpoint: str,
        model_name: str,
        token: str | None = None,
        token_file: str | None = None,
        verify_ssl: bool = True,
        timeout: float = 120.0,
    ) -> None:
        if not endpoint:
            raise LLMBackendError("KServe backend requires KSERVE_ENDPOINT to be set")
        if not model_name:
            raise LLMBackendError("KServe backend requires KSERVE_MODEL_NAME to be set")

        self.endpoint = endpoint.rstrip("/")
        self.default_model = model_name
        self._explicit_token = token
        self._token_file = Path(token_file) if token_file else None
        self.verify_ssl = verify_ssl
        self.timeout = timeout

    # -----------------------------------------------------------------
    # Auth
    # -----------------------------------------------------------------
    def _resolve_token(self) -> str | None:
        """Read the bearer token freshly on every call.

        OpenShift rotates the projected SA token roughly hourly. Caching
        it would mean spurious 401s after a rotation; the file read is
        cheap (typically a tmpfs path).
        """
        if self._explicit_token:
            return self._explicit_token
        if self._token_file and self._token_file.is_file():
            try:
                tok = self._token_file.read_text().strip()
                return tok or None
            except OSError:
                return None
        return None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        token = self._resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # -----------------------------------------------------------------
    # OpenAI-compatible chat completions
    # -----------------------------------------------------------------
    def _chat_url(self) -> str:
        return f"{self.endpoint}/v1/chat/completions"

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
            # KServe auth on RHOAI: 401 means the token wasn't sent
            # or the SA isn't bound; 403 means the SA can't reach
            # this InferenceService. Surface separately so the
            # operator knows whether to check token mount vs RBAC.
            status_code = e.response.status_code
            if status_code in (401, 403):
                raise LLMAuthError(
                    f"KServe refused auth (HTTP {status_code}) at "
                    f"{self.endpoint}. Check the service account "
                    f"token mount + RoleBinding on the inference "
                    f"namespace: {e}"
                ) from e
            raise LLMResponseError(
                f"KServe returned HTTP {status_code} at " f"{self.endpoint}: {e}"
            ) from e
        except httpx.TimeoutException as e:
            raise LLMTimeoutError(
                f"KServe request to {self.endpoint} timed out "
                f"({self.timeout}s budget). Increase "
                f"kserve_timeout_seconds or check inference pod "
                f"health: {e}"
            ) from e
        except httpx.TransportError as e:
            raise LLMUnreachableError(
                f"Cannot reach KServe at {self.endpoint}. Check "
                f"that the InferenceService route resolves from "
                f"this pod's network: {e}"
            ) from e
        except httpx.HTTPError as e:
            raise LLMBackendError(f"KServe request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise LLMResponseError(f"KServe returned non-JSON envelope: {e}") from e

        choices = body.get("choices") or []
        if not choices:
            raise LLMResponseError("KServe returned no choices in response")
        message = choices[0].get("message") or {}
        content = message.get("content", "")
        if not content:
            raise LLMResponseError("KServe returned an empty message")

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
                    resp.raise_for_status()
                    # OpenAI-compatible streams emit lines like:
                    #   data: {"choices":[{"delta":{"content":"tok"}}]}\n\n
                    #   data: [DONE]\n\n
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
            raise LLMBackendError(f"KServe stream failed: {e}") from e

    async def health_check(self) -> dict:
        started = time.monotonic()
        url = f"{self.endpoint}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=5.0, verify=self.verify_ssl) as client:
                resp = await client.get(url, headers=self._headers())
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
                "auth": "token" if self._resolve_token() else "none",
            },
        }

    def list_models(self) -> list[str]:
        # KServe InferenceServices typically expose one model per route,
        # but vLLM /v1/models returns whatever's registered. Probe the
        # endpoint; on failure fall back to the configured default since
        # callers expect at least one entry.
        try:
            with httpx.Client(timeout=5.0, verify=self.verify_ssl) as client:
                resp = client.get(f"{self.endpoint}/v1/models", headers=self._headers())
                resp.raise_for_status()
                body = resp.json()
        except (httpx.HTTPError, ValueError):
            return [self.default_model]
        names = [
            m.get("id") for m in (body.get("data") or []) if isinstance(m, dict) and m.get("id")
        ]
        return names or [self.default_model]
