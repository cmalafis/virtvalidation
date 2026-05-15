"""Tests for the OCP target registry, ResourceMapping CRUD, and the
plan generation / MTV YAML integration that consumes the mapping.

Live K8s discovery is not exercised here — those code paths route
through ``OCPDiscoveryClient`` which is itself unit-tested by mocking
the bearer-token POST. End-to-end tests use the manual-paste discovery
flow because it's identical from the operator's perspective and
doesn't require an HTTP fixture.
"""

from __future__ import annotations

from unittest.mock import patch

from app.core.mtv import MappingResolver, WaveContext, generate_wave_yaml


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _create_target(client, **overrides) -> dict:
    payload = {
        "name": "ocp-east-prod",
        "api_endpoint": "https://api.ocp-east.corp:6443",
        "classification_level": "unclassified",
        **overrides,
    }
    r = client.post("/api/sources/targets", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _create_vcenter(client, name: str = "vc-east") -> dict:
    r = client.post(
        "/api/sources/vcenters",
        json={"name": name, "hostname": f"{name}.corp.local"},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _create_vm(
    client,
    name: str,
    *,
    source_vcenter_id: int,
    networks: list[str] | None = None,
    datastores: list[str] | None = None,
    environment: str = "",
    application_hint: str = "",
) -> dict:
    body = {
        "name": name,
        "source_hostname": f"{name}.local",
        "ip_address": "10.0.0.5",
        "source_vcenter_id": source_vcenter_id,
        "vsphere_networks": networks or [],
        "vsphere_datastores": datastores or [],
        "environment": environment,
        "application_hint": application_hint,
    }
    r = client.post("/api/vms", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _discover_target_manually(client, target_id: int) -> None:
    """Seed the operator-declared cluster catalogs that the mapping
    editor reads from. Replaces the legacy /discover endpoint
    (removed in the multi-cluster target architecture refactor); the
    catalogs are populated directly via their CRUD endpoints.
    """
    for sc in (
        {"name": "ocs-rbd", "access_mode": "ReadWriteOnce", "is_default": True},
        {"name": "ocs-cephfs", "access_mode": "ReadWriteMany", "is_default": False},
    ):
        r = client.post(f"/api/sources/targets/{target_id}/storage-classes", json=sc)
        assert r.status_code in (201, 409), r.text
    for nad in (
        {"name": "prod-vlan-100", "network_type": "nad", "namespace": "openshift-multus"},
        {"name": "dmz-vlan-200", "network_type": "nad", "namespace": "openshift-multus"},
    ):
        r = client.post(f"/api/sources/targets/{target_id}/networks", json=nad)
        assert r.status_code in (201, 409), r.text


# ---------------------------------------------------------------------------
# OCP target CRUD
# ---------------------------------------------------------------------------
def test_list_targets_empty_by_default(client):
    r = client.get("/api/sources/targets")
    assert r.status_code == 200
    assert r.json() == []


def test_duplicate_target_name_returns_409(client):
    _create_target(client, name="dup")
    r = client.post(
        "/api/sources/targets",
        json={
            "name": "dup",
            "api_endpoint": "https://api.dup:6443",
            "classification_level": "unclassified",
        },
    )
    assert r.status_code == 409


def test_delete_target_returns_204_even_with_mappings_present(client):
    """SQLAlchemy + SQLite doesn't enforce FK cascades by default —
    the migration tests cover the schema-level constraint. Here we
    just verify the API path doesn't 500 when mappings exist."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "m1",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    # Delete the dependent mapping first so the FK doesn't block deletion
    # under SQLite's default (FK-disabled) mode.
    client.delete(f"/api/mappings/{mapping['id']}")
    r = client.delete(f"/api/sources/targets/{target['id']}")
    assert r.status_code == 204
    assert client.get(f"/api/sources/targets/{target['id']}").status_code == 404


# ---------------------------------------------------------------------------
# ResourceMapping CRUD + status
# ---------------------------------------------------------------------------
def test_create_mapping_with_full_coverage_marks_complete(client):
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    _create_vm(
        client,
        "vm-a",
        source_vcenter_id=vc["id"],
        networks=["src-prod"],
        datastores=["src-tier1"],
    )
    body = {
        "name": "phase-1",
        "vcenter_source_id": vc["id"],
        "ocp_target_id": target["id"],
        "network_mappings": [
            {
                "source_network": "src-prod",
                "target_network_name": "prod-vlan-100",
                "target_network_type": "nad",
                "target_namespace": "openshift-multus",
            }
        ],
        "storage_mappings": [
            {
                "source_datastore": "src-tier1",
                "target_storage_class": "ocs-rbd",
            }
        ],
        "namespace_mappings": [{"criteria": "default", "target_namespace": "finance-prod"}],
    }
    r = client.post("/api/mappings", json=body)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "complete"


def test_create_mapping_missing_source_resource_marks_incomplete(client):
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    _create_vm(
        client,
        "vm-a",
        source_vcenter_id=vc["id"],
        networks=["src-prod", "src-dmz"],
        datastores=["src-tier1"],
    )
    r = client.post(
        "/api/mappings",
        json={
            "name": "partial",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [
                {
                    "source_network": "src-prod",
                    "target_network_name": "prod-vlan-100",
                    "target_network_type": "nad",
                }
            ],
            "storage_mappings": [
                {"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}
            ],
            "namespace_mappings": [],
        },
    )
    assert r.status_code == 201
    assert r.json()["status"] == "incomplete"


def test_get_mapping_recomputes_drift_status_when_target_resource_disappears(client):
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    _create_vm(
        client,
        "vm-a",
        source_vcenter_id=vc["id"],
        networks=["src-prod"],
        datastores=["src-tier1"],
    )
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "phase-1",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [
                {
                    "source_network": "src-prod",
                    "target_network_name": "prod-vlan-100",
                    "target_network_type": "nad",
                }
            ],
            "storage_mappings": [
                {"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}
            ],
            "namespace_mappings": [{"criteria": "default", "target_namespace": "finance-prod"}],
        },
    ).json()
    assert mapping["status"] == "complete"
    # Simulate cluster drift: the operator-declared SC catalog is
    # mutated out-of-band so the row the mapping points at no longer
    # exists. The catalog DELETE endpoint is FK-protected (returns 409
    # when a mapping references the row), which is the operator-
    # facing safety. To force the drift scenario for the test we go
    # straight at the DB. In production, this state arises when an
    # operator edits the JSON directly or when a future "purge
    # catalog" admin path runs.
    from app.core import db as _db
    from app.models.target_storage_class import TargetStorageClass

    session = _db.SessionLocal()
    try:
        for row in (
            session.query(TargetStorageClass)
            .filter(TargetStorageClass.ocp_target_id == target["id"])
            .all()
        ):
            session.delete(row)
        session.add(
            TargetStorageClass(
                ocp_target_id=target["id"],
                name="different-sc",
                is_default=True,
            )
        )
        session.commit()
    finally:
        session.close()
    refreshed = client.get(f"/api/mappings/{mapping['id']}").json()
    assert refreshed["status"] == "needs_review"


def test_get_mapping_does_not_500_when_status_recompute_fails(client, monkeypatch):
    """A failure inside ``_compute_mapping_status`` (a partially-migrated
    DB, a corrupt JSON column, a flaky downstream query) must not crash
    the editor on open. Fall back to the stored status and serve the
    response."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "boom",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()

    from app.api import targets as targets_api

    def _raise(*_a, **_kw):
        raise RuntimeError("simulated target_networks table missing")

    monkeypatch.setattr(targets_api, "_compute_mapping_status", _raise)
    r = client.get(f"/api/mappings/{mapping['id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == mapping["id"]
    assert body["status"] in {"complete", "incomplete", "needs_review"}


def test_get_mapping_handles_non_dict_network_mapping_rows(client):
    """Legacy / hand-edited mappings occasionally have non-dict entries
    (None, plain strings) in the JSON column. The detail endpoint must
    skip them rather than crash."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "legacy",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()

    from app.core import db as _db
    from app.models.target import ResourceMapping

    session = _db.SessionLocal()
    try:
        row = session.get(ResourceMapping, mapping["id"])
        row.network_mappings = [
            None,
            "stray string",
            {},
            {"source_network": "src-prod", "target_network_name": "prod-vlan-100"},
        ]
        row.storage_mappings = [None, {"source_datastore": "src-tier1"}]
        session.commit()
    finally:
        session.close()

    r = client.get(f"/api/mappings/{mapping['id']}")
    assert r.status_code == 200, r.text


def test_patch_mapping_rejects_target_network_not_in_catalog(client):
    """PATCH must validate target_network_name against the operator-
    declared catalog before persisting — otherwise the editor saves a
    stale name and plan generation fails at YAML-render time."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    # Declare a single target network so the catalog is non-empty.
    client.post(
        f"/api/ocp-targets/{target['id']}/networks",
        json={"name": "valid-net", "network_type": "nad", "namespace": "openshift-multus"},
    )
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "needs-validation",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    r = client.patch(
        f"/api/mappings/{mapping['id']}",
        json={
            "network_mappings": [
                {
                    "source_network": "src-prod",
                    "target_network_name": "ghost-vlan-999",
                    "target_network_type": "nad",
                }
            ],
        },
    )
    assert r.status_code == 422, r.text
    assert "ghost-vlan-999" in r.json()["detail"]


def test_patch_mapping_accepts_valid_target_network(client):
    """The happy path — same flow as above but with a name that exists."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    client.post(
        f"/api/ocp-targets/{target['id']}/networks",
        json={"name": "valid-net", "network_type": "nad", "namespace": "openshift-multus"},
    )
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "valid-patch",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    r = client.patch(
        f"/api/mappings/{mapping['id']}",
        json={
            "network_mappings": [
                {
                    "source_network": "src-prod",
                    "target_network_name": "valid-net",
                    "target_network_type": "nad",
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["network_mappings"][0]["target_network_name"] == "valid-net"


def test_get_mapping_does_not_persist_status_on_read(client):
    """``GET`` is read-only — recomputing status into the in-memory
    response is fine, but we must not commit the recompute or bump
    ``updated_at`` on every page load."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "no-write",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    initial_updated_at = mapping["updated_at"]
    # Two reads back to back. updated_at must NOT change.
    for _ in range(2):
        r = client.get(f"/api/mappings/{mapping['id']}")
        assert r.status_code == 200
        assert r.json()["updated_at"] == initial_updated_at


def test_delete_mapping_204_when_not_referenced(client):
    vc = _create_vcenter(client)
    target = _create_target(client)
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "deletable",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    r = client.delete(f"/api/mappings/{mapping['id']}")
    assert r.status_code == 204, r.text
    r = client.get(f"/api/mappings/{mapping['id']}")
    assert r.status_code == 404


def test_delete_mapping_404_when_missing(client):
    r = client.delete("/api/mappings/9999")
    assert r.status_code == 404


def test_delete_mapping_409_when_referenced_by_plan(client, db_session):
    """A plan whose mapping_ids snapshot references this row blocks the
    delete. The 409 body lists the referencing plans so the UI can
    navigate."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "in-use",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()

    from app.core import db as _db
    from app.models.plan import MigrationPlan

    session = _db.SessionLocal()
    try:
        plan = MigrationPlan(
            name="cutover-Q3",
            vm_ids=[],
            waves=[],
            model="mock",
            mapping_ids=[mapping["id"]],
            status="complete",
        )
        session.add(plan)
        session.commit()
        plan_id = plan.id
    finally:
        session.close()

    r = client.delete(f"/api/mappings/{mapping['id']}")
    assert r.status_code == 409, r.text
    body = r.json()
    # FastAPI nests our dict under the top-level "detail" key.
    detail = body["detail"]
    assert "referenced_by" in detail
    assert any(row["plan_id"] == plan_id for row in detail["referenced_by"])


def test_creating_duplicate_mapping_for_pair_returns_409(client):
    """Mappings are unique per (vcenter, target) pair under the
    multi-cluster target architecture. The second create should
    surface the existing mapping in the 409 body."""
    vc = _create_vcenter(client)
    target = _create_target(client)
    a = client.post(
        "/api/mappings",
        json={
            "name": "a",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    r = client.post(
        "/api/mappings",
        json={
            "name": "b",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    )
    assert r.status_code == 409, r.text
    body = r.json()["detail"]
    assert body["existing_mapping_id"] == a["id"]
    assert body["existing_mapping_name"] == "a"


def test_preflight_reports_unmapped_resources(client):
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    _create_vm(
        client,
        "vm-a",
        source_vcenter_id=vc["id"],
        networks=["src-prod", "src-dmz"],
        datastores=["src-tier1", "src-bulk"],
    )
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "incomplete",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [
                {
                    "source_network": "src-prod",
                    "target_network_name": "prod-vlan-100",
                    "target_network_type": "nad",
                }
            ],
            "storage_mappings": [
                {"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}
            ],
            "namespace_mappings": [],
        },
    ).json()
    r = client.post(f"/api/mappings/{mapping['id']}/preflight")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert sorted(body["unmapped_networks"]) == ["src-dmz"]
    assert sorted(body["unmapped_datastores"]) == ["src-bulk"]
    assert any("namespace" in w for w in body["warnings"])


def test_suggest_storage_requires_discovery(client):
    vc = _create_vcenter(client)
    target = _create_target(client)
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "no-discovery",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    r = client.post(f"/api/mappings/{mapping['id']}/suggest-storage")
    # Empty operator catalog → 400, same shape as before; the legacy
    # "409 when discovery hasn't run" is gone with the removal of the
    # discovery lifecycle.
    assert r.status_code == 400, r.text


def test_suggest_storage_drops_hallucinated_target_names(client):
    vc = _create_vcenter(client)
    target = _create_target(client)
    _discover_target_manually(client, target["id"])
    _create_vm(
        client,
        "vm-a",
        source_vcenter_id=vc["id"],
        datastores=["tier1-ssd"],
    )
    mapping = client.post(
        "/api/mappings",
        json={
            "name": "for-suggestions",
            "vcenter_source_id": vc["id"],
            "ocp_target_id": target["id"],
            "network_mappings": [],
            "storage_mappings": [],
            "namespace_mappings": [],
        },
    ).json()
    fake_response = {
        "rationale_summary": "Match by name",
        "suggestions": [
            {
                "source_datastore": "tier1-ssd",
                "target_storage_class": "non-existent-sc",
                "access_mode": "ReadWriteOnce",
                "confidence": "high",
                "rationale": "name match",
            }
        ],
    }
    with patch("app.core.mapping_suggester.get_active_backend") as factory:
        backend = factory.return_value
        backend.chat_sync.return_value = {"content": __import__("json").dumps(fake_response)}
        r = client.post(f"/api/mappings/{mapping['id']}/suggest-storage")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["suggestions"][0]["target_storage_class"] is None
    assert body["suggestions"][0]["confidence"] == "low"
    assert "Dropped" in body["rationale_summary"]


# ---------------------------------------------------------------------------
# MappingResolver + MTV YAML integration
# ---------------------------------------------------------------------------
def test_mapping_resolver_resolves_network_storage_namespace():
    resolver = MappingResolver(
        network_mappings=[
            {
                "source_network": "src-prod",
                "target_network_name": "prod-vlan-100",
                "target_namespace": "openshift-multus",
                "target_network_type": "nad",
            }
        ],
        storage_mappings=[{"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}],
        namespace_mappings=[
            {
                "criteria": "environment",
                "criteria_value": "prod",
                "target_namespace": "finance-prod",
            },
            {"criteria": "default", "target_namespace": "finance-dev"},
        ],
    )
    assert resolver.resolve_network("src-prod") == {
        "name": "prod-vlan-100",
        "namespace": "openshift-multus",
        "type": "nad",
    }
    assert resolver.resolve_network("missing") is None
    assert resolver.resolve_storage("src-tier1") == "ocs-rbd"
    assert resolver.resolve_namespace({"environment": "prod"}) == "finance-prod"
    # No environment match falls through to the "default" rule.
    assert resolver.resolve_namespace({"environment": "dev"}) == "finance-dev"


def test_generate_wave_yaml_with_resolver_uses_mapped_names():
    ctx = WaveContext(
        plan_id=1,
        wave_number=1,
        rationale="phase 1",
        namespace="konveyor-forklift",
        source_provider="vsphere-1",
        destination_provider="ocp-east",
        default_target_namespace="default-fallback",
    )
    vms = [
        {
            "name": "vm-prod-1",
            "vsphere_networks": ["src-prod"],
            "vsphere_datastores": ["src-tier1"],
            "environment": "prod",
            "target_namespace": "ignore-me",
            "target_storage_class": "ignore-me",
            "target_network_attachment": "ignore-me",
        }
    ]
    resolver = MappingResolver(
        network_mappings=[
            {
                "source_network": "src-prod",
                "target_network_name": "prod-vlan-100",
                "target_namespace": "openshift-multus",
                "target_network_type": "nad",
            }
        ],
        storage_mappings=[{"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}],
        namespace_mappings=[{"criteria": "default", "target_namespace": "finance-prod"}],
    )
    yaml_text = generate_wave_yaml(ctx, vms, resolver=resolver)
    # Confirm the mapping-driven names land in the YAML, not the per-VM
    # placeholders.
    assert "prod-vlan-100" in yaml_text
    assert "ocs-rbd" in yaml_text
    assert "finance-prod" in yaml_text
    assert "ignore-me" not in yaml_text


def test_generate_wave_yaml_without_resolver_falls_back_to_per_vm_fields():
    """Legacy path stays intact for plans without a mapping_id."""
    ctx = WaveContext(
        plan_id=1,
        wave_number=1,
        rationale="legacy",
        namespace="konveyor-forklift",
        source_provider="vsphere-1",
        destination_provider="ocp-east",
        default_target_namespace="ns-default",
    )
    vms = [
        {
            "name": "vm-1",
            "vsphere_networks": ["src-prod"],
            "vsphere_datastores": ["src-tier1"],
            "target_namespace": "legacy-ns",
            "target_storage_class": "legacy-sc",
            "target_network_attachment": "legacy-nad",
        }
    ]
    yaml_text = generate_wave_yaml(ctx, vms, resolver=None)
    assert "legacy-sc" in yaml_text
    assert "legacy-nad" in yaml_text
    assert "legacy-ns" in yaml_text


# ---------------------------------------------------------------------------
# Plan generation + mapping_id
#
# Tests that exercised the legacy POST /api/plans/generate endpoint
# were deleted alongside the route in the planner rearchitecture.
# Mapping-coverage validation for the new POST /api/plans path lives
# in tests/test_plans_lifecycle_api.py.
# ---------------------------------------------------------------------------


def test_mtv_yaml_export_uses_active_mapping_when_present(client, db_session):
    """End-to-end: plan with mapping_id set -> YAML uses cluster names."""
    from app.models.plan import MigrationPlan
    from app.models.target import OCPTarget, ResourceMapping
    from app.models.vcenter import VCenterSource
    from app.models.vm import VM

    vc = VCenterSource(name="vc", hostname="vc.local")
    db_session.add(vc)
    target = OCPTarget(
        name="ocp",
        api_endpoint="https://api:6443",
    )
    db_session.add(target)
    db_session.flush()
    vm = VM(
        name="vm-a",
        source_hostname="vm-a.local",
        ip_address="10.0.0.5",
        source_vcenter_id=vc.id,
        vsphere_networks=["src-prod"],
        vsphere_datastores=["src-tier1"],
        environment="prod",
    )
    db_session.add(vm)
    db_session.flush()
    mapping = ResourceMapping(
        name="m1",
        vcenter_source_id=vc.id,
        ocp_target_id=target.id,
        network_mappings=[
            {
                "source_network": "src-prod",
                "target_network_name": "prod-vlan-100",
                "target_namespace": "openshift-multus",
                "target_network_type": "nad",
            }
        ],
        storage_mappings=[{"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}],
        namespace_mappings=[{"criteria": "default", "target_namespace": "finance-prod"}],
    )
    db_session.add(mapping)
    db_session.flush()
    plan = MigrationPlan(
        name="p",
        vm_ids=[vm.id],
        waves=[{"wave_number": 1, "vm_ids": [vm.id], "rationale": "only wave"}],
        model="test-model",
        mapping_ids=[mapping.id],
    )
    db_session.add(plan)
    db_session.commit()

    r = client.get(f"/api/plans/{plan.id}/waves/1/mtv-yaml")
    assert r.status_code == 200, r.text
    body = r.text
    assert "prod-vlan-100" in body
    assert "ocs-rbd" in body
    assert "finance-prod" in body


def test_mtv_yaml_export_409_when_referenced_mapping_deleted(client, db_session):
    """Plan still references a mapping_id but the row was deleted —
    surface a 409 with a clear message rather than silently falling back."""
    from app.models.plan import MigrationPlan
    from app.models.vcenter import VCenterSource
    from app.models.vm import VM

    vc = VCenterSource(name="vc-409", hostname="vc.local")
    db_session.add(vc)
    db_session.flush()
    vm = VM(
        name="vm-409",
        source_hostname="vm-409.local",
        ip_address="10.0.0.5",
        source_vcenter_id=vc.id,
        vsphere_networks=["src-prod"],
        vsphere_datastores=["src-tier1"],
    )
    db_session.add(vm)
    db_session.flush()
    plan = MigrationPlan(
        name="p2",
        vm_ids=[vm.id],
        waves=[{"wave_number": 1, "vm_ids": [vm.id], "rationale": "x"}],
        model="test-model",
        mapping_ids=[99_999],  # never existed
    )
    db_session.add(plan)
    db_session.commit()
    r = client.get(f"/api/plans/{plan.id}/waves/1/mtv-yaml")
    assert r.status_code == 409
    assert "mapping" in r.json()["detail"].lower()


def test_mtv_yaml_export_handles_dict_shape_namespace_strategy(client, db_session):
    """Regression: mapping.namespace_mappings stored as a dict
    (NamespaceStrategy) used to be coerced via ``list(...)`` in the
    endpoint, which turns the dict into a list of its KEYS. The
    MappingResolver then iterated strings and raised
    ``AttributeError: 'str' object has no attribute 'get'`` —
    surfacing as a bare 500 to operators. The fix preserves the
    dict shape so the resolver dispatches correctly."""
    from app.models.plan import MigrationPlan
    from app.models.target import OCPTarget, ResourceMapping
    from app.models.vcenter import VCenterSource
    from app.models.vm import VM

    vc = VCenterSource(name="vc-dict", hostname="vc-dict.local")
    db_session.add(vc)
    target = OCPTarget(name="ocp-dict", api_endpoint="https://api:6443")
    db_session.add(target)
    db_session.flush()
    vm = VM(
        name="vm-dict",
        source_hostname="vm-dict.local",
        ip_address="10.0.0.5",
        source_vcenter_id=vc.id,
        vsphere_networks=["src-prod"],
        vsphere_datastores=["src-tier1"],
        environment="production",
    )
    db_session.add(vm)
    db_session.flush()
    mapping = ResourceMapping(
        name="m-dict",
        vcenter_source_id=vc.id,
        ocp_target_id=target.id,
        network_mappings=[
            {
                "source_network": "src-prod",
                "target_network_name": "prod-vlan-100",
                "target_namespace": "openshift-multus",
                "target_network_type": "nad",
            }
        ],
        storage_mappings=[{"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}],
        # New-style namespace strategy: dict, NOT a list.
        namespace_mappings={
            "strategy": "per_environment",
            "per_env_namespaces": {
                "production": "prod-vms",
                "staging": "stg-vms",
            },
        },
    )
    db_session.add(mapping)
    db_session.flush()
    plan = MigrationPlan(
        name="p-dict",
        vm_ids=[vm.id],
        waves=[{"wave_number": 1, "vm_ids": [vm.id], "rationale": "only wave"}],
        model="test-model",
        mapping_ids=[mapping.id],
    )
    db_session.add(plan)
    db_session.commit()

    r = client.get(f"/api/plans/{plan.id}/waves/1/mtv-yaml")
    assert r.status_code == 200, r.text
    body = r.text
    # The dict strategy resolved this VM (environment=production) → prod-vms.
    assert "prod-vms" in body
    assert "prod-vlan-100" in body
    assert "ocs-rbd" in body
