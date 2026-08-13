"""Canonical names for the LLM backend types VirtValidate supports.

The enum values are the strings used everywhere — env vars, DB rows,
API payloads, factory dispatch. ``LLMBackendType.maas.value == "maas"``.
Adding a new backend means adding a member here, then a factory dispatch
case + a concrete ``LLMBackend`` subclass.

Why an enum, not a free-form string: the active backend is now a DB
column with an Enum type and a Pydantic schema with ``use_enum_values``
serialization — both need a single source of truth. The factory's
``_SUPPORTED_BACKENDS`` derives from this enum so adding a member
auto-extends the validation gate.
"""

from __future__ import annotations

import enum


class LLMBackendType(str, enum.Enum):
    ollama = "ollama"
    kserve = "kserve"
    vllm = "vllm"
    mock = "mock"
    maas = "maas"
    # TrustyAI Guardrails Orchestrator (RHOAI) — proxies an OpenAI-compatible
    # model through input/output detectors. See trustyai_backend.py.
    trustyai = "trustyai"
