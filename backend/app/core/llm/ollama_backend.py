"""Ollama backend — default for standalone, air-gapped deployments.

Talks to the Ollama HTTP API at ``$OLLAMA_HOST``. Wraps the existing
single-instance Ollama deployment that ships with podman-compose.
"""

from __future__ import annotations

import json
import time
from typing import AsyncIterator

import httpx

from app.core.llm.base import LLMBackend, LLMBackendError


class OllamaBackend(LLMBackend):
    backend_type = "ollama"

    def __init__(
        self,
        base_url: str = "http://ollama:11434",
        default_model: str = "llama3:8b",
        timeout: float = 120.0,
    ) -> None:
        self.endpoint = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout = timeout

    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        payload: dict = {
            "model": model or self.default_model,
            "stream": False,
            # Ollama's structured-output mode — guarantees valid JSON
            # comes back when the orchestrator asked for JSON.
            "format": "json",
            "options": {"temperature": temperature},
            "messages": messages,
        }
        if max_tokens is not None:
            # Ollama uses num_predict, not max_tokens.
            payload["options"]["num_predict"] = max_tokens

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(f"{self.endpoint}/api/chat", json=payload)
                resp.raise_for_status()
        except httpx.HTTPError as e:
            raise LLMBackendError(f"Ollama request failed: {e}") from e

        try:
            body = resp.json()
        except ValueError as e:
            raise LLMBackendError(f"Ollama returned non-JSON envelope: {e}") from e

        message = body.get("message") or {}
        content = message.get("content", "")
        if not content:
            raise LLMBackendError("Ollama returned an empty message")

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
            "stream": True,
            "options": {"temperature": temperature},
            "messages": messages,
        }
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Ollama emits one JSON object per line; iter_lines is the
                # right primitive here, not iter_text.
                async with client.stream(
                    "POST", f"{self.endpoint}/api/chat", json=payload
                ) as resp:
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
            m["name"]
            for m in (body.get("models") or [])
            if isinstance(m, dict) and m.get("name")
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
            m["name"]
            for m in (body.get("models") or [])
            if isinstance(m, dict) and m.get("name")
        ]
