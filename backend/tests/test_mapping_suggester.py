"""Rules-first mapping suggestions with LLM validate-retry-fallback."""

from __future__ import annotations

import json

from app.core.llm.base import LLMAuthError, LLMBackend, LLMBackendError
from app.core.mapping_suggester import (
    suggest_network_mappings,
    suggest_storage_mappings,
    vlan_from_name,
)

TARGETS = [
    {"name": "db-vlan-100", "type": "nad", "namespace": "fin", "vlan_id": 100},
    {"name": "vlan-200", "type": "cudn", "namespace": None, "topology": "localnet", "vlan_id": 200},
    {"name": "tenant-blue", "type": "cudn", "namespace": None, "role": "primary"},
    {"name": "dmz", "type": "nad", "namespace": "edge"},
]


class ScriptedBackend(LLMBackend):
    """Replays canned replies; records every prompt it saw."""

    backend_type = "scripted"
    default_model = "scripted"
    endpoint = "memory://scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def chat(self, messages, model=None, temperature=0.1, max_tokens=None, **kw):
        return self.chat_sync(messages)

    def chat_sync(self, messages, model=None, temperature=0.1, max_tokens=None, **kw):
        self.prompts.append(messages[-1]["content"])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return {"content": reply if isinstance(reply, str) else json.dumps(reply), "model": "s"}

    async def chat_stream(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def health_check(self):  # pragma: no cover
        return {}

    async def list_models(self):  # pragma: no cover
        return []


def _rows(result):
    return {s["source_network"]: s for s in result["suggestions"]}


def test_vlan_from_name():
    assert vlan_from_name("PG-VLAN100-DB") == 100
    assert vlan_from_name("v200") == 200
    assert vlan_from_name("Management") is None


def test_rules_need_no_llm():
    sources = [
        {"name": "DB-PG", "vm_count": 3, "vlan_id": 100},
        {"name": "VLAN200", "vm_count": 1},
        {"name": "DMZ", "vm_count": 2},
    ]
    backend = ScriptedBackend([])  # any call would IndexError
    rows = _rows(suggest_network_mappings(sources=sources, targets=TARGETS, backend=backend))
    assert rows["DB-PG"]["target_network_name"] == "db-vlan-100"
    assert rows["DB-PG"]["method"] == "rule:vlan" and rows["DB-PG"]["confidence"] == "high"
    assert rows["VLAN200"]["method"] == "rule:vlan"
    assert rows["DMZ"]["method"] == "rule:name"
    assert backend.prompts == []


def test_llm_batches_are_at_most_ten_with_at_most_ten_candidates():
    sources = [{"name": f"app-{i}", "vm_count": 1} for i in range(23)]
    targets = [{"name": f"net-{i}", "type": "nad", "namespace": "x"} for i in range(15)]

    def reply_for(prompt):
        batch = json.loads(prompt.split("SOURCES_JSON: ")[1].splitlines()[0])
        short = json.loads(prompt.split("SHORTLIST_JSON: ")[1].splitlines()[0])
        assert len(batch) <= 10 and len(short) <= 10
        return {
            "suggestions": [
                {
                    "source": s["name"],
                    "target": short[0]["name"],
                    "confidence": "medium",
                    "rationale": "r",
                }
                for s in batch
            ]
        }

    class Echo(ScriptedBackend):
        def chat_sync(self, messages, **kw):
            self.prompts.append(messages[-1]["content"])
            return {"content": json.dumps(reply_for(messages[-1]["content"])), "model": "e"}

    backend = Echo([])
    result = suggest_network_mappings(sources=sources, targets=targets, backend=backend)
    assert len(backend.prompts) == 3  # 10 + 10 + 3
    assert result["methods"] == {"llm": 23}


def test_retry_feeds_back_the_validation_error():
    sources = [{"name": "Weird-Seg", "vm_count": 1}]
    bad = {"suggestions": [{"source": "Weird-Seg", "target": "made-up"}]}
    good = {"suggestions": [{"source": "Weird-Seg", "target": "dmz", "confidence": "low"}]}
    backend = ScriptedBackend(["not json", bad, good])
    rows = _rows(suggest_network_mappings(sources=sources, targets=TARGETS, backend=backend))
    assert rows["Weird-Seg"]["method"] == "llm_retry_2"
    assert rows["Weird-Seg"]["target_network_name"] == "dmz"
    assert backend.prompts[1].startswith("YOUR PREVIOUS ANSWER WAS REJECTED: not valid JSON")
    assert "not in the SHORTLIST" in backend.prompts[2].splitlines()[0]


def test_three_failures_fall_back_without_guessing_a_network():
    sources = [{"name": "Weird-Seg", "vm_count": 1}]
    backend = ScriptedBackend(["x", "y", "z"])
    rows = _rows(suggest_network_mappings(sources=sources, targets=TARGETS, backend=backend))
    assert rows["Weird-Seg"]["method"] == "mechanical_fallback"
    assert rows["Weird-Seg"]["target_network_name"] is None


def test_auth_failure_sets_banner_and_falls_back(monkeypatch):
    recorded = []
    monkeypatch.setattr("app.core.mapping_suggester.record_last_llm_error", recorded.append)
    sources = [{"name": "Weird-Seg", "vm_count": 1}]
    backend = ScriptedBackend([LLMAuthError("401")])
    rows = _rows(suggest_network_mappings(sources=sources, targets=TARGETS, backend=backend))
    assert rows["Weird-Seg"]["method"] == "mechanical_fallback_auth"
    assert recorded and "authentication failed" in recorded[0]


def test_transport_error_falls_back_quietly():
    backend = ScriptedBackend([LLMBackendError("timeout")])
    result = suggest_network_mappings(
        sources=[{"name": "Weird-Seg", "vm_count": 1}], targets=TARGETS, backend=backend
    )
    assert result["methods"] == {"mechanical_fallback": 1}
    assert len(backend.prompts) == 1


def test_storage_fallback_uses_the_default_class():
    targets = [
        {"name": "ceph-rbd", "provisioner": "rbd", "is_default": True},
        {"name": "nfs", "provisioner": "nfs", "is_default": False},
    ]
    backend = ScriptedBackend(["x", "y", "z"])
    result = suggest_storage_mappings(
        sources=[{"name": "vsanDatastore", "vm_count": 9}], targets=targets, backend=backend
    )
    row = result["suggestions"][0]
    assert (row["target_storage_class"], row["method"], row["confidence"]) == (
        "ceph-rbd",
        "mechanical_fallback",
        "low",
    )


def test_single_storage_class_is_a_rule():
    result = suggest_storage_mappings(
        sources=[{"name": "ds1", "vm_count": 1}, {"name": "ds2", "vm_count": 1}],
        targets=[{"name": "only", "is_default": False}],
        backend=ScriptedBackend([]),
    )
    assert result["methods"] == {"rule:single_target": 2}


def test_platform_lines_reach_the_prompt():
    backend = ScriptedBackend([{"suggestions": [{"source": "Weird-Seg", "target": None}]}])
    suggest_network_mappings(
        sources=[{"name": "Weird-Seg", "vm_count": 1}],
        targets=TARGETS,
        backend=backend,
        platform_lines=["Target: OpenShift 4.22, MTV 2.12"],
    )
    assert "Target: OpenShift 4.22, MTV 2.12" in backend.prompts[0]


def test_name_rule_ignores_numbers_when_unambiguous():
    targets = [
        {"name": "prod-vlan", "type": "nad", "namespace": "x"},
        {"name": "app-vlan", "type": "nad", "namespace": "x"},
    ]
    sources = [{"name": "prod-vlan-100", "vm_count": 1}, {"name": "ehr-vlan-116", "vm_count": 1}]
    backend = ScriptedBackend(
        [
            {
                "suggestions": [
                    {"source": "ehr-vlan-116", "target": "app-vlan", "confidence": "medium"}
                ]
            }
        ]
    )
    rows = _rows(suggest_network_mappings(sources=sources, targets=targets, backend=backend))
    assert (rows["prod-vlan-100"]["target_network_name"], rows["prod-vlan-100"]["method"]) == (
        "prod-vlan",
        "rule:name",
    )
    assert rows["ehr-vlan-116"]["method"] == "llm"


def test_parser_tolerates_repeated_keys_and_echoed_sources():
    sources = [{"name": "Seg-A", "vm_count": 1}, {"name": "Seg-B", "vm_count": 1}]
    # Exactly what llama3.2:3b produced on the sandbox: one "suggestions" key
    # per item, and the source echoed as the whole SOURCES object.
    raw = (
        '{"suggestions": [{"source": {"name": "Seg-A", "vlan_id": null}, "target": "dmz",'
        ' "confidence": "low"}], "suggestions": [{"source": "Seg-B", "target": "null"}]}'
    )
    backend = ScriptedBackend([raw])
    rows = _rows(suggest_network_mappings(sources=sources, targets=TARGETS, backend=backend))
    assert rows["Seg-A"]["target_network_name"] == "dmz" and rows["Seg-A"]["method"] == "llm"
    assert rows["Seg-B"]["target_network_name"] is None
    assert '"vm_count"' not in backend.prompts[0]


def test_parser_strips_copied_placeholder_brackets():
    raw = {"suggestions": [{"source": "<Seg-A>", "target": "<dmz>", "confidence": "low"}]}
    rows = _rows(
        suggest_network_mappings(
            sources=[{"name": "Seg-A", "vm_count": 1}],
            targets=TARGETS,
            backend=ScriptedBackend([raw]),
        )
    )
    assert (rows["Seg-A"]["target_network_name"], rows["Seg-A"]["method"]) == ("dmz", "llm")


def test_answers_for_unknown_sources_are_ignored():
    raw = {
        "suggestions": [
            {"source": "Seg-A", "target": "dmz", "confidence": "low"},
            {"source": "vmotion-pg", "target": None},  # the prompt's own example
        ]
    }
    rows = _rows(
        suggest_network_mappings(
            sources=[{"name": "Seg-A", "vm_count": 1}],
            targets=TARGETS,
            backend=ScriptedBackend([raw]),
        )
    )
    assert rows["Seg-A"]["method"] == "llm" and set(rows) == {"Seg-A"}


def test_targets_answered_by_candidate_id():
    def reply(prompt):
        short = json.loads(prompt.split("SHORTLIST_JSON: ")[1].splitlines()[0])
        dmz = next(c["id"] for c in short if c["name"] == "dmz")
        return {"suggestions": [{"source": "Seg-A", "target": dmz, "confidence": "low"}]}

    class ById(ScriptedBackend):
        def chat_sync(self, messages, **kw):
            self.prompts.append(messages[-1]["content"])
            return {"content": json.dumps(reply(messages[-1]["content"])), "model": "i"}

    rows = _rows(
        suggest_network_mappings(
            sources=[{"name": "Seg-A", "vm_count": 1}], targets=TARGETS, backend=ById([])
        )
    )
    assert (rows["Seg-A"]["target_network_name"], rows["Seg-A"]["method"]) == ("dmz", "llm")


def test_echoed_source_as_target_is_rejected_then_retried():
    bad = {"suggestions": [{"source": "Seg-A", "target": "Seg-A"}]}
    good = {"suggestions": [{"source": "Seg-A", "target": "C1"}]}
    backend = ScriptedBackend([bad, good])
    rows = _rows(
        suggest_network_mappings(
            sources=[{"name": "Seg-A", "vm_count": 1}], targets=TARGETS, backend=backend
        )
    )
    assert rows["Seg-A"]["method"] == "llm_retry_1"
    assert "candidate id" in backend.prompts[1].splitlines()[0]
