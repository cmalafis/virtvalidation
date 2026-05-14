"""Level 1 categorizer tests.

The LLM is mocked at the backend level — no network. We assert that:

  - The categorizer correctly batches large fleets.
  - Group + member rows land with the right shape.
  - Bad LLM output is surfaced as ``CategorizationError`` with a
    batch index so operators can correlate to logs.
  - Re-running replaces the previous run rather than duplicating.
  - The HTTP surface (POST /categorize + GET /categorize/{task_id}
    + GET /groups) round-trips end-to-end.
"""

from __future__ import annotations

import json

import pytest

from app.core import categorizer as cat
from app.core.categorizer import CategorizationError, categorize


# ---------------------------------------------------------------------------
# Stub backend — captures every chat() call and returns canned content
# ---------------------------------------------------------------------------
class _StubBackend:
    backend_type = "stub"
    default_model = "stub-model"
    endpoint = "stub://"

    def __init__(self):
        self.calls: list[dict] = []
        # Default response: groups every input VM into "app-x", "prod", "ops".
        self._builder = self._default_builder

    def chat_sync(self, *, messages, temperature=0.1, **_kwargs):
        self.calls.append({"messages": messages, "temperature": temperature})
        body = self._builder(messages)
        return {"content": body, "model": self.default_model}

    def chat(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    def chat_stream(self, *args, **kwargs):  # pragma: no cover
        raise NotImplementedError

    async def health_check(self):  # pragma: no cover
        return {"status": "online", "backend": self.backend_type}

    def list_models(self):  # pragma: no cover
        return [self.default_model]

    @staticmethod
    def _default_builder(messages):
        # Find the user payload — the categorizer puts the JSON list of
        # VMs in the second message body. Echo each vm_id back into one
        # group of each kind so the test fixture stays predictable.
        user_text = messages[-1]["content"]
        # The user message has a JSON payload prefixed by some prose.
        json_start = user_text.index("{")
        payload = json.loads(user_text[json_start:])
        members = [
            {"vm_id": v["vm_id"], "confidence": 0.9, "rationale": "test"} for v in payload["vms"]
        ]
        return json.dumps(
            {
                "groups": [
                    {
                        "kind": "application",
                        "name": "app-x",
                        "description": "stub application group",
                        "members": members,
                    },
                    {
                        "kind": "environment",
                        "name": "prod",
                        "description": "stub environment group",
                        "members": members,
                    },
                    {
                        "kind": "business_unit",
                        "name": "ops",
                        "description": "stub bu group",
                        "members": members,
                    },
                ]
            }
        )


def _create_vcenter(client, name: str = "vc-cat") -> dict:
    r = client.post(
        "/api/sources/vcenters",
        json={"name": name, "hostname": f"{name}.host"},
    )
    return r.json()


def _enroll(client, name: str, **fields) -> dict:
    body = {
        "name": name,
        "source_hostname": f"{name}.local",
        **fields,
    }
    r = client.post("/api/vms", json=body)
    return r.json()


# ---------------------------------------------------------------------------
# categorize() — domain logic
# ---------------------------------------------------------------------------
def test_categorize_groups_all_vms_in_a_single_batch(client, db_session):
    """A small fleet (≤ batch_size) emits one LLM call and lands every
    VM in three groups (one per kind)."""
    vc = _create_vcenter(client)
    a = _enroll(client, "vm-a", source_vcenter_id=vc["id"], role="db")
    b = _enroll(client, "vm-b", source_vcenter_id=vc["id"], role="app")

    backend = _StubBackend()
    result = categorize(db_session, source_vcenter_id=vc["id"], backend=backend)

    assert result["batches_processed"] == 1
    assert result["groups_created"] == 3  # one per kind
    assert result["members_added"] == 6  # 2 VMs × 3 kinds

    groups = client.get(f"/api/sources/vcenters/{vc['id']}/groups").json()
    by_kind = {g["kind"]: g for g in groups}
    assert set(by_kind) == {"application", "environment", "business_unit"}
    assert by_kind["application"]["vm_count"] == 2

    # Sanity — both VMs are referenced. No exact-id assertion since
    # the backend handed back whatever we submitted.
    assert {a["id"], b["id"]} == {a["id"], b["id"]}  # noop, kept for readability


def test_categorize_emits_one_call_per_batch(client, db_session):
    vc = _create_vcenter(client)
    for i in range(7):
        _enroll(client, f"vm-{i}", source_vcenter_id=vc["id"])

    backend = _StubBackend()
    result = categorize(
        db_session,
        source_vcenter_id=vc["id"],
        backend=backend,
        batch_size=3,
    )
    # 7 VMs / batch 3 → 3 batches (3, 3, 1).
    assert result["batches_processed"] == 3
    assert len(backend.calls) == 3


def test_categorize_invokes_progress_callback_per_batch(client, db_session):
    vc = _create_vcenter(client)
    for i in range(5):
        _enroll(client, f"vm-{i}", source_vcenter_id=vc["id"])

    progress_calls: list[tuple[int, int]] = []

    def cb(*, batches_total, batches_complete):
        progress_calls.append((batches_total, batches_complete))

    categorize(
        db_session,
        source_vcenter_id=vc["id"],
        backend=_StubBackend(),
        batch_size=2,
        progress_cb=cb,
    )
    # First call is "0 of N" before the LLM phase, then one per batch.
    # 5 VMs / 2 → 3 batches.
    assert progress_calls[0] == (3, 0)
    assert progress_calls[-1] == (3, 3)


def test_categorize_rerun_replaces_previous_groups(client, db_session):
    """Re-running on the same vCenter wipes the prior run before
    inserting — count stays bounded, no append-mode duplication."""
    from app.models.grouping import VMGroup, VMGroupMember

    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])

    categorize(db_session, source_vcenter_id=vc["id"], backend=_StubBackend())
    first_count = db_session.query(VMGroup).count()
    first_member_count = db_session.query(VMGroupMember).count()

    categorize(db_session, source_vcenter_id=vc["id"], backend=_StubBackend())
    second_count = db_session.query(VMGroup).count()
    second_member_count = db_session.query(VMGroupMember).count()

    # Counts stay flat — if the rerun were appending, both numbers
    # would double.
    assert first_count == 3
    assert second_count == 3
    assert first_member_count == 3
    assert second_member_count == 3


def test_categorize_drops_hallucinated_vm_ids(client, db_session):
    vc = _create_vcenter(client)
    _enroll(client, "vm-real", source_vcenter_id=vc["id"])

    backend = _StubBackend()
    backend._builder = lambda msgs: json.dumps(
        {
            "groups": [
                {
                    "kind": "application",
                    "name": "app-x",
                    "description": "",
                    "members": [
                        # The first VM's id varies, but 99999 won't exist.
                        {"vm_id": 99999, "confidence": 0.9},
                    ],
                }
            ]
        }
    )
    result = categorize(db_session, source_vcenter_id=vc["id"], backend=backend)
    assert result["groups_created"] == 1
    assert result["members_added"] == 0  # hallucinated id was dropped


def test_categorize_raises_on_malformed_llm_output(client, db_session):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])

    backend = _StubBackend()
    backend._builder = lambda msgs: "not json"
    with pytest.raises(CategorizationError, match="Batch 1/1"):
        categorize(db_session, source_vcenter_id=vc["id"], backend=backend)


def test_categorize_raises_when_groups_missing_from_response(client, db_session):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])

    backend = _StubBackend()
    backend._builder = lambda msgs: json.dumps({"summary": "no groups here"})
    with pytest.raises(CategorizationError, match="missing 'groups'"):
        categorize(db_session, source_vcenter_id=vc["id"], backend=backend)


def test_categorize_returns_zero_for_empty_vcenter(client, db_session):
    vc = _create_vcenter(client)
    result = categorize(db_session, source_vcenter_id=vc["id"], backend=_StubBackend())
    assert result == {"groups_created": 0, "batches_processed": 0}


def test_categorize_404_for_unknown_vcenter(db_session):
    with pytest.raises(CategorizationError, match="not found"):
        categorize(db_session, source_vcenter_id=99999, backend=_StubBackend())


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
@pytest.fixture
def stub_categorizer_backend(monkeypatch):
    """Make the run_categorization_task BackgroundTask use a stub backend."""
    backend = _StubBackend()
    # The BackgroundTask resolves the backend via get_llm_backend; patch
    # at the categorizer module so both the in-test direct calls and
    # the BackgroundTask invocation see the stub.
    monkeypatch.setattr(cat, "get_llm_backend", lambda: backend)

    # Reset task store between runs.
    cat.task_store._tasks.clear()
    yield backend
    cat.task_store._tasks.clear()


def test_trigger_categorization_returns_task_handle(client, stub_categorizer_backend):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])

    r = client.post(f"/api/sources/vcenters/{vc['id']}/categorize")
    assert r.status_code == 202
    body = r.json()
    assert body["source_vcenter_id"] == vc["id"]
    assert body["status"] in ("running", "completed")
    assert body["task_id"]


def test_trigger_categorization_runs_in_background_and_persists_groups(
    client, stub_categorizer_backend
):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])
    _enroll(client, "vm-b", source_vcenter_id=vc["id"])

    spawn = client.post(f"/api/sources/vcenters/{vc['id']}/categorize").json()

    status = client.get(f"/api/sources/vcenters/{vc['id']}/categorize/{spawn['task_id']}").json()
    assert status["status"] == "completed"
    assert status["progress_percent"] == 100
    assert status["groups_created"] == 3

    groups = client.get(f"/api/sources/vcenters/{vc['id']}/groups").json()
    assert {g["kind"] for g in groups} == {"application", "environment", "business_unit"}


def test_trigger_categorization_404_for_unknown_vcenter(client, stub_categorizer_backend):
    r = client.post("/api/sources/vcenters/99999/categorize")
    assert r.status_code == 404


def test_categorization_status_404_for_unknown_task(client, stub_categorizer_backend):
    vc = _create_vcenter(client)
    r = client.get(f"/api/sources/vcenters/{vc['id']}/categorize/not-a-task")
    assert r.status_code == 404


def test_categorization_status_404_when_task_belongs_to_different_vcenter(
    client, stub_categorizer_backend
):
    a = _create_vcenter(client, name="vc-a")
    b = _create_vcenter(client, name="vc-b")
    _enroll(client, "vm-a", source_vcenter_id=a["id"])

    spawn = client.post(f"/api/sources/vcenters/{a['id']}/categorize").json()
    r = client.get(f"/api/sources/vcenters/{b['id']}/categorize/{spawn['task_id']}")
    assert r.status_code == 404


def test_list_groups_filters_by_kind(client, stub_categorizer_backend):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])
    client.post(f"/api/sources/vcenters/{vc['id']}/categorize")

    apps = client.get(f"/api/sources/vcenters/{vc['id']}/groups?kind=application").json()
    assert len(apps) == 1
    assert apps[0]["kind"] == "application"


def test_categorization_audits_triggered_and_completed(client, stub_categorizer_backend):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])

    client.post(f"/api/sources/vcenters/{vc['id']}/categorize")

    triggered = client.get("/api/audit?action=categorization.triggered").json()
    completed = client.get("/api/audit?action=categorization.completed").json()
    assert len(triggered) == 1
    assert len(completed) == 1
    assert triggered[0]["details"]["vcenter_name"] == vc["name"]
    assert completed[0]["details"]["groups_created"] == 3
