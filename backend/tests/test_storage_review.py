"""Tests for the Storage Design Review feature.

Mirrors test_network_review.py structure. Two layers:
  - ``StorageReviewer`` parser/validator with mocked LLM responses
  - End-to-end API: create → upload notes/yaml → analyze → fetch
    findings → triage. Background analysis runs through a stubbed
    analyzer so the test never hits the network.
"""

from __future__ import annotations

import json

import pytest

from app.core.storage_review import (
    StorageReviewer,
    StorageReviewError,
    build_storage_source_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
SAMPLE_LLM_OUTPUT = {
    "executive_summary": (
        "The proposed design preserves performance tier alignment for the "
        "production database tier but loses array-level replication and "
        "lacks a VolumeSnapshotClass for the general-purpose StorageClass."
    ),
    "findings": [
        {
            "category": "performance_tier_mismatch",
            "severity": "high",
            "title": "Production DB tier mapped to general-purpose StorageClass",
            "description": (
                "Customer notes mention 'production databases need <5ms latency' but "
                "the proposed StorageClass 'standard' is shared general-purpose."
            ),
            "source_evidence": "Customer notes: 'production databases need <5ms latency'",
            "proposed_evidence": "StorageClass standard (no perf tier annotation)",
            "recommendation": "Provision a high-performance StorageClass (NVMe-backed, fsync<5ms) and route DB VMs there.",
            "confidence": "high",
        },
        {
            "category": "replication_loss",
            "severity": "critical",
            "title": "Replicated source array has no replication on target",
            "description": (
                "Customer notes describe 'datastore is on replicated array' for the "
                "production tier; proposed StorageClass has no replication parameter."
            ),
            "source_evidence": "Customer notes: 'datastore is on replicated array'",
            "proposed_evidence": "(absent — no replication config in proposed StorageClass)",
            "recommendation": "Enable CSI driver replication or pair StorageClass with backup tooling.",
            "confidence": "high",
        },
        {
            "category": "snapshot_capability",
            "severity": "medium",
            "title": "VolumeSnapshotClass missing for proposed StorageClass",
            "description": (
                "VMs rely on VMware snapshots for backup; proposed YAML has no "
                "VolumeSnapshotClass referencing the new StorageClass."
            ),
            "source_evidence": "VMware snapshots in source environment",
            "proposed_evidence": "(no VolumeSnapshotClass in proposed YAML)",
            "recommendation": "Add a VolumeSnapshotClass bound to the CSI driver in use.",
            "confidence": "high",
        },
        {
            "category": "access_mode_mismatch",
            "severity": "high",
            "title": "Shared-disk RAC VMs mapped to ReadWriteOnce",
            "description": (
                "Source VMs share VMDKs across nodes (Oracle RAC pattern) but the "
                "proposed StorageClass advertises ReadWriteOnce only."
            ),
            "source_evidence": "vsphere_datastores: shared-rac-disks (3 VMs)",
            "proposed_evidence": "StorageClass standard accessModes: [ReadWriteOnce]",
            "recommendation": "Use a StorageClass that supports ReadWriteMany (e.g. CephFS) for the RAC tier.",
            "confidence": "medium",
        },
        {
            "category": "positive_confirmation",
            "severity": "info",
            "title": "Capacity headroom adequate for consolidated storage",
            "description": (
                "Source datastore total ~12TB; proposed StorageClass quota ~20TB."
            ),
            "source_evidence": "vsphere_datastores: prod-tier (12TB used)",
            "proposed_evidence": "StorageClass quota: 20Ti",
            "recommendation": "No action needed.",
            "confidence": "high",
        },
    ],
}

SAMPLE_NOTES = """\
# Source storage environment notes

Production databases need <5ms latency. Datastore 'prod-array-tier1' is on
a replicated array (synchronous replication to DR site). VMware snapshots
are used for daily backups. Multipath policy is round-robin across two
fabrics.
"""

SAMPLE_YAML = """\
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: openshift-storage.rbd.csi.ceph.com
parameters:
  pool: rbd-pool
allowVolumeExpansion: true
reclaimPolicy: Delete
"""


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace StorageReviewer._chat with a deterministic JSON return."""

    def fake_chat(self, system, user):
        return json.dumps(SAMPLE_LLM_OUTPUT)

    monkeypatch.setattr(StorageReviewer, "_chat", fake_chat)


# ---------------------------------------------------------------------------
# Source-summary builder
# ---------------------------------------------------------------------------
def test_build_storage_source_summary_aggregates_datastores(client, db_session):
    """Two VMs sharing a datastore collapse into one entry; multi-datastore
    spread is counted separately so the LLM can flag HA preservation."""
    client.post(
        "/api/vms",
        json={
            "name": "db-01",
            "source_hostname": "db-01.local",
            "vsphere_datastores": ["nfs-prod-fast", "nfs-prod-replica"],
        },
    )
    client.post(
        "/api/vms",
        json={
            "name": "db-02",
            "source_hostname": "db-02.local",
            "vsphere_datastores": ["nfs-prod-fast"],
        },
    )

    summary = build_storage_source_summary(db_session)
    assert summary["total_vms"] == 2
    ds = {n["name"]: n for n in summary["vsphere_datastores"]}
    assert ds["nfs-prod-fast"]["vm_count"] == 2
    assert ds["nfs-prod-replica"]["vm_count"] == 1
    assert "db-01" in ds["nfs-prod-fast"]["sample_vms"]
    # db-01 spans 2 datastores; counted once.
    assert summary["vms_spanning_multiple_datastores"] == 1


def test_build_storage_source_summary_empty_inventory(db_session):
    summary = build_storage_source_summary(db_session)
    assert summary["total_vms"] == 0
    assert summary["vsphere_datastores"] == []
    assert summary["vms_spanning_multiple_datastores"] == 0


# ---------------------------------------------------------------------------
# Parser validation
# ---------------------------------------------------------------------------
def test_parser_accepts_well_formed_output():
    result = StorageReviewer._parse(json.dumps(SAMPLE_LLM_OUTPUT))
    assert "replication" in result["executive_summary"].lower()
    assert len(result["findings"]) == 5
    cats = {f["category"] for f in result["findings"]}
    # Spot-check that storage-specific categories survived parsing.
    assert "performance_tier_mismatch" in cats
    assert "replication_loss" in cats
    assert "snapshot_capability" in cats
    assert "access_mode_mismatch" in cats
    assert "positive_confirmation" in cats


def test_parser_normalizes_unknown_confidence_to_medium():
    payload = {
        **SAMPLE_LLM_OUTPUT,
        "findings": [
            {**SAMPLE_LLM_OUTPUT["findings"][0], "confidence": "definitely-yes"}
        ],
    }
    result = StorageReviewer._parse(json.dumps(payload))
    assert result["findings"][0]["confidence"] == "medium"


def test_parser_rejects_unknown_category():
    payload = {
        **SAMPLE_LLM_OUTPUT,
        "findings": [{**SAMPLE_LLM_OUTPUT["findings"][0], "category": "made-up"}],
    }
    with pytest.raises(StorageReviewError, match="category"):
        StorageReviewer._parse(json.dumps(payload))


def test_parser_rejects_network_categories_on_storage_review():
    """Storage uses storage-specific categories — network categories
    like 'coverage_gap' must NOT be accepted, otherwise LLM drift between
    the two reviewers would silently mis-classify."""
    payload = {
        **SAMPLE_LLM_OUTPUT,
        "findings": [
            {**SAMPLE_LLM_OUTPUT["findings"][0], "category": "coverage_gap"}
        ],
    }
    with pytest.raises(StorageReviewError, match="category"):
        StorageReviewer._parse(json.dumps(payload))


def test_parser_rejects_unknown_severity():
    payload = {
        **SAMPLE_LLM_OUTPUT,
        "findings": [{**SAMPLE_LLM_OUTPUT["findings"][0], "severity": "extreme"}],
    }
    with pytest.raises(StorageReviewError, match="severity"):
        StorageReviewer._parse(json.dumps(payload))


def test_parser_rejects_non_object_output():
    with pytest.raises(StorageReviewError, match="JSON object"):
        StorageReviewer._parse(json.dumps([1, 2, 3]))


def test_parser_rejects_garbage_json():
    with pytest.raises(StorageReviewError, match="not valid JSON"):
        StorageReviewer._parse("{not valid")


# ---------------------------------------------------------------------------
# End-to-end API: create → notes → yaml → analyze → fetch
# ---------------------------------------------------------------------------
def test_create_review_round_trips(client):
    body = client.post(
        "/api/storage-reviews", json={"name": "Storage cutover review"}
    ).json()
    assert body["status"] == "draft"
    assert body["customer_notes"] == ""
    assert body["proposed_yaml"] == ""
    listing = client.get("/api/storage-reviews").json()
    assert listing[0]["id"] == body["id"]
    assert listing[0]["finding_count"] == 0


def test_notes_and_yaml_uploads_persist(client):
    rid = client.post("/api/storage-reviews", json={"name": "test"}).json()["id"]
    client.put(
        f"/api/storage-reviews/{rid}/notes",
        json={"customer_notes": SAMPLE_NOTES},
    )
    client.put(
        f"/api/storage-reviews/{rid}/yaml",
        json={"proposed_yaml": SAMPLE_YAML},
    )
    body = client.get(f"/api/storage-reviews/{rid}").json()
    assert "<5ms latency" in body["customer_notes"]
    assert "StorageClass" in body["proposed_yaml"]


def test_analyze_persists_findings_with_correct_categories(client, stub_llm):
    rid = client.post(
        "/api/storage-reviews",
        json={
            "name": "phase-1-storage",
            "customer_notes": SAMPLE_NOTES,
            "proposed_yaml": SAMPLE_YAML,
        },
    ).json()["id"]

    r = client.post(f"/api/storage-reviews/{rid}/analyze")
    assert r.status_code == 202

    body = client.get(f"/api/storage-reviews/{rid}").json()
    assert body["status"] == "completed"
    assert "replication" in body["analysis_results"]["executive_summary"].lower()
    assert len(body["findings"]) == 5

    cats = {f["category"] for f in body["findings"]}
    assert "replication_loss" in cats
    assert "performance_tier_mismatch" in cats
    sevs = {f["severity"] for f in body["findings"]}
    assert "critical" in sevs
    assert "high" in sevs


def test_analyze_re_run_replaces_old_findings(client, stub_llm):
    rid = client.post("/api/storage-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/storage-reviews/{rid}/analyze")
    first = client.get(f"/api/storage-reviews/{rid}").json()
    first_ids = {f["id"] for f in first["findings"]}

    client.post(f"/api/storage-reviews/{rid}/analyze")
    second = client.get(f"/api/storage-reviews/{rid}").json()
    second_ids = {f["id"] for f in second["findings"]}
    # Old rows replaced — different DB ids.
    assert first_ids.isdisjoint(second_ids)
    assert len(second["findings"]) == len(first["findings"])


def test_analyze_marks_failure_on_llm_error(client, monkeypatch):
    def boom(self, system, user):
        raise StorageReviewError("ollama unreachable")

    monkeypatch.setattr(StorageReviewer, "_chat", boom)

    rid = client.post("/api/storage-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/storage-reviews/{rid}/analyze")
    body = client.get(f"/api/storage-reviews/{rid}").json()
    assert body["status"] == "failed"
    assert "ollama unreachable" in body["last_error"]


def test_finding_triage_round_trip(client, stub_llm):
    rid = client.post("/api/storage-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/storage-reviews/{rid}/analyze")
    findings = client.get(f"/api/storage-reviews/{rid}").json()["findings"]
    fid = findings[0]["id"]

    r = client.patch(
        f"/api/storage-reviews/{rid}/findings/{fid}",
        json={"triage": "accepted"},
    )
    assert r.status_code == 200
    assert r.json()["triage"] == "accepted"

    r = client.patch(
        f"/api/storage-reviews/{rid}/findings/{fid}",
        json={"triage": "dismissed"},
    )
    assert r.json()["triage"] == "dismissed"


def test_delete_review_cascades_findings(client, stub_llm):
    rid = client.post("/api/storage-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/storage-reviews/{rid}/analyze")
    assert len(client.get(f"/api/storage-reviews/{rid}").json()["findings"]) > 0

    r = client.delete(f"/api/storage-reviews/{rid}")
    assert r.status_code == 204
    assert client.get(f"/api/storage-reviews/{rid}").status_code == 404


def test_audit_log_records_create_analyze_delete(client, stub_llm):
    rid = client.post("/api/storage-reviews", json={"name": "phase-1"}).json()["id"]
    client.post(f"/api/storage-reviews/{rid}/analyze")
    client.delete(f"/api/storage-reviews/{rid}")

    actions = {
        row["action"]
        for row in client.get("/api/audit?limit=50").json()
        if row.get("resource_type") == "storage_review"
    }
    assert {
        "storage_review.create",
        "storage_review.analyze_triggered",
        "storage_review.delete",
    } <= actions


def test_storage_and_network_reviews_are_isolated(client, stub_llm):
    """Independent endpoints — listing one shouldn't show the other."""
    client.post("/api/network-reviews", json={"name": "net-only"})
    client.post("/api/storage-reviews", json={"name": "stor-only"})

    storage_listing = client.get("/api/storage-reviews").json()
    network_listing = client.get("/api/network-reviews").json()

    assert {r["name"] for r in storage_listing} == {"stor-only"}
    assert {r["name"] for r in network_listing} == {"net-only"}
