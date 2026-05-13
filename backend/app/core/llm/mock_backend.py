"""Mock LLM backend — instant canned responses for local dev + CI.

Real backends (Ollama, KServe, vLLM) are too slow for iterating on
chunker / planner / UI flow at 1,000-VM scale. A Llama 3 8B pass over
1K VMs runs hours on a CPU host; that loop is unworkable for fast
local iteration.

MockBackend short-circuits the transport layer. It inspects the prompt
content, infers intent (categorize / plan / validate / topology), and
returns a deterministic JSON response in the **exact shape the real
orchestrator parses** (`app/core/categorizer.py`, `app/core/planner.py`,
`app/core/llm/client.py`).

Hard rules:
  - This is dev-only. ``LLM_BACKEND_TYPE=mock`` must NEVER be set in
    production — `info()` and `health_check()` both surface a
    "responses are canned" warning so the operator can't accidentally
    demo it as real reasoning.
  - Responses match the verbatim schema callers parse; deviations show
    up as PlannerError / LLMError / CategorizerError downstream.
  - Output is deterministic for identical input — same prompt in, same
    JSON out, so tests pinning the mock can rely on byte-identical
    responses across runs.
"""

from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator

from app.core.llm.base import LLMBackend

logger = logging.getLogger(__name__)


# Intent ordering matters — the categorizer prompt contains the word
# "group" repeatedly but is the categorization intent, not planning.
# The first match wins; place more-specific tokens earlier.
_INTENT_KEYWORDS: list[tuple[str, list[str]]] = [
    (
        "categorize",
        [
            "level 1 categorizer",
            "application group",
            "business_unit",
            'kind": "application',
            "categorize",
        ],
    ),
    (
        "wave_rationale",
        [
            # Batched per-wave rationale prompt covers up to 10 waves per
            # call and asks for a JSON map of wave_number → rationale.
            # Must come BEFORE the broader plan/plan_groups intents so it
            # doesn't get swallowed by either.
            "generate rationale text for the following",
            "expected wave_numbers in your output",
        ],
    ),
    (
        "plan_groups",
        [
            # The group-based planner emits group_ids in both prompt + schema,
            # which the raw-VM planner does NOT — so this is a reliable
            # discriminator. Must come BEFORE "plan" so the latter doesn't
            # eat the match.
            "group_ids",
            "pre-formed groups to assign",
            "pre-formed vm groups",
        ],
    ),
    (
        "plan",
        [
            "wave_number",
            "wave plan",
            "migration plan",
            "planner",
            "waves to migrate",
        ],
    ),
    (
        "topology",
        [
            "topology",
            "load_balanced",
            "ha pair",
            "cluster pattern",
        ],
    ),
    (
        "validate",
        [
            "post-migration",
            "verdict",
            "diff vs baseline",
            "validate",
            "findings",
        ],
    ),
]


# vm_id pattern in categorizer payloads ("vm_id": 42).
_VM_ID_RE = re.compile(r'"vm_id"\s*:\s*(\d+)')

# VM name pattern in either categorizer ("name": "db-prod-01") or
# planner ("name": "db-prod-01") prompts.
_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')


def _detect_intent(text: str) -> str:
    lowered = text.lower()
    for intent, tokens in _INTENT_KEYWORDS:
        for token in tokens:
            if token.lower() in lowered:
                return intent
    return "generic"


def _extract_vm_ids(text: str) -> list[int]:
    """Pull every vm_id integer out of a categorizer prompt.

    Order-preserved, deduplicated. Returns at most 200 entries — the
    mock keeps responses bounded so callers don't blow up the test
    fixtures.
    """
    seen: set[int] = set()
    out: list[int] = []
    for match in _VM_ID_RE.finditer(text):
        vid = int(match.group(1))
        if vid in seen:
            continue
        seen.add(vid)
        out.append(vid)
        if len(out) >= 200:
            break
    return out


def _extract_vm_names(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for match in _NAME_RE.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        out.append(name)
        if len(out) >= 200:
            break
    return out


class MockBackend(LLMBackend):
    """In-process canned-response backend for local dev / CI.

    See module docstring for the contract. ``chat_sync`` is overridden
    so the mock doesn't pay the ``asyncio.run`` round-trip — under load
    that overhead is what we're trying to avoid.
    """

    backend_type = "mock"
    default_model = "mock-llm"
    endpoint = "memory://mock"

    # Generous defaults so the planner doesn't artificially chunk at
    # mock-test sizes. 32K context covers any payload the real Llama
    # 3 8B backend would have to chunk.
    max_planning_chunk_size: int = 50
    max_context_tokens: int = 32_000
    supports_concurrent_calls: bool = True
    max_concurrent_calls: int = 5

    def __init__(self) -> None:
        # No-op; mock keeps no connection state.
        pass

    # ------------------------------------------------------------------
    # Public LLMBackend surface
    # ------------------------------------------------------------------
    async def chat(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        return self._build_response(messages, model=model)

    def chat_sync(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict:
        # Override the ABC's asyncio.run bridge — the mock doesn't need
        # a loop and shouldn't pay the spin-up cost on every call.
        return self._build_response(messages, model=model)

    async def chat_stream(
        self,
        messages: list[dict],
        model: str | None = None,
        temperature: float = 0.1,
    ) -> AsyncIterator[str]:
        # No real streaming — yield the canned content in one chunk.
        content = self._build_response(messages, model=model)["content"]
        yield content

    async def health_check(self) -> dict:
        return {
            "status": "online",
            "backend": self.backend_type,
            "model": self.default_model,
            "endpoint": self.endpoint,
            "latency_ms": 0,
            "details": {
                "purpose": "local development testing",
                "warning": (
                    "responses are canned, not real LLM reasoning — "
                    "do not use for customer demos or quality validation"
                ),
            },
        }

    def list_models(self) -> list[str]:
        return [self.default_model]

    def info(self) -> dict:
        base = super().info()
        base["warning"] = "Mock backend: canned responses, not real LLM reasoning."
        return base

    # ------------------------------------------------------------------
    # Internal — response builder
    # ------------------------------------------------------------------
    def _build_response(self, messages: list[dict], *, model: str | None) -> dict:
        prompt_text = self._concat(messages)
        intent = _detect_intent(prompt_text)
        if intent == "categorize":
            payload = self._categorization_response(prompt_text)
        elif intent == "wave_rationale":
            payload = self._wave_rationale_response(prompt_text)
        elif intent == "plan_groups":
            payload = self._planning_groups_response(prompt_text)
        elif intent == "plan":
            payload = self._planning_response(prompt_text)
        elif intent == "validate":
            payload = self._validation_response()
        elif intent == "topology":
            payload = self._topology_response(prompt_text)
        else:
            payload = self._generic_response()
        return {
            "content": json.dumps(payload),
            "model": model or self.default_model,
        }

    @staticmethod
    def _concat(messages: list[dict]) -> str:
        return "\n".join(
            (m.get("content") or "") if isinstance(m.get("content"), str) else "" for m in messages
        )

    # ------------------------------------------------------------------
    # Per-intent canned response generators
    # ------------------------------------------------------------------
    def _categorization_response(self, prompt: str) -> dict:
        """Real categorizer schema: every vm_id appears in one application,
        one environment, and one business_unit group. The mock packs every
        observed vm_id into the same canned trio so the parser accepts
        the response on inputs of any size."""
        vm_ids = _extract_vm_ids(prompt)
        members = [
            {
                "vm_id": vid,
                "confidence": 0.85,
                "rationale": "mock: deterministic canned grouping",
            }
            for vid in vm_ids
        ]
        return {
            "groups": [
                {
                    "kind": "application",
                    "name": "mock-app",
                    "description": "Mock application group — canned by MockBackend",
                    "members": members,
                },
                {
                    "kind": "environment",
                    "name": "mock-env",
                    "description": "Mock environment group — canned by MockBackend",
                    "members": members,
                },
                {
                    "kind": "business_unit",
                    "name": "mock-bu",
                    "description": "Mock business unit — canned by MockBackend",
                    "members": members,
                },
            ]
        }

    def _wave_rationale_response(self, prompt: str) -> dict:
        """Batched per-wave rationale.

        Returns a ``{rationales: [{wave_number, rationale}, ...]}`` dict
        whose entries match every wave_number the prompt declares. The
        planner's batched parser keys responses back to waves by
        wave_number, so tests can pin both single-batch and
        multi-batch behavior.
        """
        import re as _re

        expected_match = _re.search(
            r"Expected wave_numbers in your output:\s*\[([0-9,\s]+)\]",
            prompt,
        )
        if expected_match:
            wave_numbers = [int(x) for x in expected_match.group(1).split(",") if x.strip()]
        else:
            wave_numbers = sorted({int(m) for m in _re.findall(r"Wave\s+(\d+)\b", prompt)})
        if not wave_numbers:
            wave_numbers = [1]
        return {
            "rationales": [
                {
                    "wave_number": wn,
                    "rationale": (
                        f"Mock rationale for wave {wn}: groups are placed "
                        "here by deterministic role + dependency ordering. "
                        "Stateful tiers migrate before dependent stateless "
                        "tiers; HA peers are spread across consecutive "
                        "waves to preserve quorum. Watch for connection "
                        "drain timing during cutover."
                    ),
                }
                for wn in wave_numbers
            ],
        }

    def _planning_groups_response(self, prompt: str) -> dict:
        """Group-based planner schema lives in app/core/planner.py.

        Output schema: ``{summary, waves: [{wave_number, group_ids,
        rationale, estimated_risk}]}``. Every group_id from the prompt
        must appear in exactly one wave — same invariant the raw-VM
        planner has, just at the group level.

        The mock buckets groups into two waves so the planner's
        multi-wave integrity check is exercised even on synthetic
        input. Roles drive the ordering: stateful + infrastructure
        first, web/edge last, everything else in the middle wave.
        """
        # Extract group_ids from the prompt. They look like
        # "id": "vc1/prod/web/stateless/hint:foo:web" in the JSON.
        import re as _re

        ids = _re.findall(r'"id"\s*:\s*"([^"]+)"', prompt)
        # Dedup preserving order so identical inputs produce identical
        # outputs.
        seen: set[str] = set()
        unique: list[str] = []
        for gid in ids:
            if gid not in seen:
                seen.add(gid)
                unique.append(gid)
        if not unique:
            return {
                "summary": "Mock plan: no group ids visible in prompt.",
                "waves": [
                    {
                        "wave_number": 1,
                        "group_ids": [],
                        "rationale": "mock: empty input",
                        "estimated_risk": "low",
                    }
                ],
            }
        # Two-wave bucket: foundations (data + infrastructure) first,
        # everything else second. Falls back to single wave when only
        # one bucket has members.
        foundations = [g for g in unique if "/data/" in g or "/infrastructure/" in g]
        rest = [g for g in unique if g not in foundations]
        waves: list[dict] = []
        if foundations and rest:
            waves.append(
                {
                    "wave_number": 1,
                    "group_ids": foundations,
                    "rationale": "mock: stateful + infrastructure groups migrate first",
                    "estimated_risk": "high",
                }
            )
            waves.append(
                {
                    "wave_number": 2,
                    "group_ids": rest,
                    "rationale": "mock: stateless dependents follow foundations",
                    "estimated_risk": "low",
                }
            )
        else:
            waves.append(
                {
                    "wave_number": 1,
                    "group_ids": unique,
                    "rationale": "mock: single-wave migration (no stateful/stateless split detected)",
                    "estimated_risk": "medium",
                }
            )
        return {
            "summary": "Mock plan generated by test backend (group-based path).",
            "waves": waves,
        }

    def _planning_response(self, prompt: str) -> dict:
        """Real planner schema: every vm_id appears in exactly one wave.

        The mock buckets the input into two waves (stateful first, then
        the rest) so the integration tests exercise the multi-wave code
        path even though the assignment is canned. If only one vm_id is
        present, returns a single wave."""
        vm_ids = _extract_vm_ids(prompt)
        if not vm_ids:
            # Planner refuses empty wave lists. Emit a single placeholder
            # wave so the parser stays happy; real callers always pass
            # at least one VM.
            return {
                "summary": "Mock plan: no VM ids visible in prompt.",
                "waves": [
                    {
                        "wave_number": 1,
                        "vm_ids": [],
                        "rationale": "mock: empty input",
                        "estimated_risk": "low",
                    }
                ],
            }
        if len(vm_ids) == 1:
            return {
                "summary": "Mock plan: single-VM cutover.",
                "waves": [
                    {
                        "wave_number": 1,
                        "vm_ids": list(vm_ids),
                        "rationale": "mock: foundational VM migrates first",
                        "estimated_risk": "low",
                    }
                ],
            }
        # Split: first VM in wave 1 ("foundation"), rest in wave 2.
        return {
            "summary": "Mock plan generated by test backend.",
            "waves": [
                {
                    "wave_number": 1,
                    "vm_ids": [vm_ids[0]],
                    "rationale": "mock: foundational VM migrates first",
                    "estimated_risk": "low",
                },
                {
                    "wave_number": 2,
                    "vm_ids": vm_ids[1:],
                    "rationale": "mock: dependent VMs follow the foundation wave",
                    "estimated_risk": "medium",
                },
            ],
        }

    def _validation_response(self) -> dict:
        """Real validator schema lives in app/core/llm/client.py.

        The mock returns the "pass" verdict with empty findings so the
        validator parser accepts it without invoking the retry path.
        Mock validations never simulate failures — operators iterating
        on the validation flow should drive the failure path with a
        crafted diff in the orchestrator's unit tests, not by tuning
        the LLM backend."""
        return {
            "status": "pass",
            "summary": (
                "Mock: no concerning changes detected. Validation backend "
                "is in mock mode; this verdict is canned and does not "
                "reflect real diff analysis."
            ),
            "findings": [],
            "remediation": [],
        }

    def _topology_response(self, prompt: str) -> dict:
        """Topology detection — surfaces patterns like load_balanced /
        ha_pair. The current product doesn't depend on a fixed parser
        here, so the mock returns a permissive shape that includes the
        canonical fields the spec calls out."""
        vm_ids = _extract_vm_ids(prompt) or [1]
        return {
            "detected_patterns": [
                {
                    "type": "load_balanced",
                    "members": [{"vm_id": vid, "role": "instance"} for vid in vm_ids],
                    "confidence": 0.85,
                    "reasoning": "mock: deterministic canned topology",
                }
            ]
        }

    @staticmethod
    def _generic_response() -> dict:
        return {
            "summary": "Mock response from MockBackend",
            "notes": [
                "Intent fell through to generic — prompt did not match any "
                "known orchestrator pattern.",
                "Set LLM_BACKEND_TYPE to ollama/kserve/vllm for real reasoning.",
            ],
        }
