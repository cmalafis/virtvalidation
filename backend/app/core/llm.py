"""
LLM Client — Ollama (local, air-gapped)
Sends VM state diffs to local Llama 3 for reasoning and verdict generation.
"""
import httpx
import os

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3:8b")


class LLMClient:
    def validate(self, baseline: dict, current_state: dict, vm_role: str) -> dict:
        """Reason over pre/post migration diff and return structured verdict."""
        raise NotImplementedError("LLM validation engine — coming in v0.2")
