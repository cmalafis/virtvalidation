"""Tests for the Network Design Review feature.

Two layers:
  - ``NetworkReviewer`` parser/validator with mocked Ollama responses
  - End-to-end API: create → upload notes/yaml → analyze → fetch findings
    → triage. Background analysis runs through a stubbed analyzer so the
    test never hits the network.
"""

from __future__ import annotations

import json

import pytest

from app.core.network_review import NetworkReviewer, NetworkReviewError, build_source_summary

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
SAMPLE_LLM_OUTPUT = {
    "executive_summary": (
        "The proposed design covers the main production segment but loses "
        "DMZ separation and is missing a backup network."
    ),
    "findings": [
        {
            "category": "coverage_gap",
            "severity": "high",
            "title": "DMZ portgroup absent from proposed CUDN set",
            "description": "Source has a 'DMZ' portgroup with 4 VMs; no matching CUDN appears in the proposed YAML.",
            "source_evidence": "vsphere_networks: DMZ (4 VMs)",
            "proposed_evidence": "(absent)",
            "recommendation": "Add a CUDN named dmz-net with appropriate NetworkPolicy isolation.",
            "confidence": "high",
        },
        {
            "category": "config_mismatch",
            "severity": "medium",
            "title": "MTU drift between source and proposed network",
            "description": "Customer notes specify jumbo frames (MTU 9000) on the storage network; proposed NAD declares MTU 1500.",
            "source_evidence": "Customer notes: 'storage segment uses jumbo frames'",
            "proposed_evidence": "spec.config.mtu: 1500",
            "recommendation": "Update NAD MTU to 9000 to match source storage network.",
            "confidence": "medium",
        },
        {
            "category": "missing_resource",
            "severity": "critical",
            "title": "No NetworkPolicy enforcing DMZ isolation",
            "description": "Customer notes describe a hard DMZ boundary; proposed YAML has no NetworkPolicy.",
            "source_evidence": "Customer notes: 'DMZ must be air-gapped from internal'",
            "proposed_evidence": "(no NetworkPolicy resources in proposed YAML)",
            "recommendation": "Add a NetworkPolicy denying ingress from the internal namespace.",
            "confidence": "high",
        },
        {
            "category": "positive_confirmation",
            "severity": "info",
            "title": "Frontend portgroup correctly mapped",
            "description": "Source 'API Frontend' portgroup is correctly mapped to a NAD in the proposed design.",
            "source_evidence": "vsphere_networks: API Frontend (8 VMs)",
            "proposed_evidence": "NetworkAttachmentDefinition api-frontend-nad",
            "recommendation": "No action needed.",
            "confidence": "high",
        },
    ],
}

SAMPLE_NOTES = """\
# Source environment notes

The DMZ must be air-gapped from internal networks. Storage segment uses
jumbo frames (MTU 9000). Application tier shares a portgroup with the
load balancers — that's intentional.
"""

SAMPLE_YAML = """\
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: api-frontend-nad
  namespace: finance-prod
spec:
  config: |
    {"cniVersion":"0.3.1","type":"bridge","mtu":1500}
"""


@pytest.fixture
def stub_ollama(monkeypatch):
    """Replace NetworkReviewer._chat with a deterministic JSON return."""

    def fake_chat(self, system, user):
        return json.dumps(SAMPLE_LLM_OUTPUT)

    monkeypatch.setattr(NetworkReviewer, "_chat", fake_chat)


# ---------------------------------------------------------------------------
# Source-summary builder
# ---------------------------------------------------------------------------
def test_build_source_summary_aggregates_portgroups(client, db_session):
    """Two VMs sharing a portgroup should collapse into one entry with vm_count=2."""
    client.post(
        "/api/vms",
        json={
            "name": "db-01",
            "source_hostname": "db-01.local",
            "ip_address": "10.0.0.5",
            "vsphere_networks": ["DB Backend", "VM Network"],
            "vsphere_datastores": ["nfs-prod-fast"],
        },
    )
    client.post(
        "/api/vms",
        json={
            "name": "db-02",
            "source_hostname": "db-02.local",
            "ip_address": "10.0.0.6",
            "vsphere_networks": ["DB Backend"],
            "vsphere_datastores": ["nfs-prod-fast"],
        },
    )

    summary = build_source_summary(db_session)
    assert summary["total_vms"] == 2
    pg = {n["name"]: n for n in summary["vsphere_networks"]}
    assert pg["DB Backend"]["vm_count"] == 2
    assert pg["VM Network"]["vm_count"] == 1
    assert "db-01" in pg["DB Backend"]["sample_vms"]
    assert summary["vsphere_datastores"][0]["name"] == "nfs-prod-fast"


# ---------------------------------------------------------------------------
# Parser validation
# ---------------------------------------------------------------------------
def test_parser_accepts_well_formed_output():
    result = NetworkReviewer._parse(json.dumps(SAMPLE_LLM_OUTPUT))
    assert "DMZ" in result["executive_summary"]
    assert len(result["findings"]) == 4
    categories = {f["category"] for f in result["findings"]}
    assert categories == {
        "coverage_gap",
        "config_mismatch",
        "missing_resource",
        "positive_confirmation",
    }


def test_parser_normalizes_unknown_confidence_to_medium():
    payload = {
        **SAMPLE_LLM_OUTPUT,
        "findings": [{**SAMPLE_LLM_OUTPUT["findings"][0], "confidence": "definitely-yes"}],
    }
    result = NetworkReviewer._parse(json.dumps(payload))
    assert result["findings"][0]["confidence"] == "medium"


def test_parser_rejects_unknown_category():
    payload = {
        **SAMPLE_LLM_OUTPUT,
        "findings": [{**SAMPLE_LLM_OUTPUT["findings"][0], "category": "made-up"}],
    }
    with pytest.raises(NetworkReviewError, match="category"):
        NetworkReviewer._parse(json.dumps(payload))


def test_parser_rejects_unknown_severity():
    payload = {
        **SAMPLE_LLM_OUTPUT,
        "findings": [{**SAMPLE_LLM_OUTPUT["findings"][0], "severity": "extreme"}],
    }
    with pytest.raises(NetworkReviewError, match="severity"):
        NetworkReviewer._parse(json.dumps(payload))


def test_parser_rejects_non_object_output():
    with pytest.raises(NetworkReviewError, match="JSON object"):
        NetworkReviewer._parse(json.dumps([1, 2, 3]))


def test_parser_rejects_garbage_json():
    with pytest.raises(NetworkReviewError, match="not valid JSON"):
        NetworkReviewer._parse("{not valid")


# ---------------------------------------------------------------------------
# End-to-end API: create → notes → yaml → analyze → fetch
# ---------------------------------------------------------------------------
def test_create_review_round_trips(client):
    body = client.post("/api/network-reviews", json={"name": "Phase 1 cutover"}).json()
    assert body["status"] == "draft"
    assert body["customer_notes"] == ""
    assert body["proposed_yaml"] == ""
    listing = client.get("/api/network-reviews").json()
    assert listing[0]["id"] == body["id"]
    assert listing[0]["finding_count"] == 0


def test_notes_and_yaml_uploads_persist(client):
    rid = client.post("/api/network-reviews", json={"name": "test"}).json()["id"]
    client.put(f"/api/network-reviews/{rid}/notes", json={"customer_notes": SAMPLE_NOTES})
    client.put(f"/api/network-reviews/{rid}/yaml", json={"proposed_yaml": SAMPLE_YAML})
    body = client.get(f"/api/network-reviews/{rid}").json()
    assert "DMZ" in body["customer_notes"]
    assert "api-frontend-nad" in body["proposed_yaml"]


def test_analyze_persists_findings_with_correct_categories(client, stub_ollama):
    """Drives the full pipeline. With TestClient, BackgroundTasks run
    before the response returns, so the next GET sees status=completed."""
    rid = client.post(
        "/api/network-reviews",
        json={
            "name": "phase-1",
            "customer_notes": SAMPLE_NOTES,
            "proposed_yaml": SAMPLE_YAML,
        },
    ).json()["id"]

    r = client.post(f"/api/network-reviews/{rid}/analyze")
    assert r.status_code == 202

    body = client.get(f"/api/network-reviews/{rid}").json()
    assert body["status"] == "completed"
    assert "DMZ" in body["analysis_results"]["executive_summary"]
    assert len(body["findings"]) == 4

    cats = sorted(f["category"] for f in body["findings"])
    assert cats == ["config_mismatch", "coverage_gap", "missing_resource", "positive_confirmation"]
    sevs = {f["severity"] for f in body["findings"]}
    assert "critical" in sevs
    assert "high" in sevs


def test_analyze_re_run_replaces_old_findings(client, stub_ollama):
    rid = client.post("/api/network-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/network-reviews/{rid}/analyze")
    first = client.get(f"/api/network-reviews/{rid}").json()
    first_ids = {f["id"] for f in first["findings"]}

    client.post(f"/api/network-reviews/{rid}/analyze")
    second = client.get(f"/api/network-reviews/{rid}").json()
    second_ids = {f["id"] for f in second["findings"]}
    # Old rows replaced — different DB ids.
    assert first_ids.isdisjoint(second_ids)
    assert len(second["findings"]) == len(first["findings"])


def test_analyze_marks_failure_on_llm_error(client, monkeypatch):
    def boom(self, system, user):
        raise NetworkReviewError("ollama unreachable")

    monkeypatch.setattr(NetworkReviewer, "_chat", boom)

    rid = client.post("/api/network-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/network-reviews/{rid}/analyze")
    body = client.get(f"/api/network-reviews/{rid}").json()
    assert body["status"] == "failed"
    assert "ollama unreachable" in body["last_error"]


def test_finding_triage_round_trip(client, stub_ollama):
    rid = client.post("/api/network-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/network-reviews/{rid}/analyze")
    findings = client.get(f"/api/network-reviews/{rid}").json()["findings"]
    fid = findings[0]["id"]

    r = client.patch(f"/api/network-reviews/{rid}/findings/{fid}", json={"triage": "accepted"})
    assert r.status_code == 200
    assert r.json()["triage"] == "accepted"

    r = client.patch(f"/api/network-reviews/{rid}/findings/{fid}", json={"triage": "dismissed"})
    assert r.json()["triage"] == "dismissed"


def test_delete_review_cascades_findings(client, stub_ollama):
    rid = client.post("/api/network-reviews", json={"name": "test"}).json()["id"]
    client.post(f"/api/network-reviews/{rid}/analyze")
    assert len(client.get(f"/api/network-reviews/{rid}").json()["findings"]) > 0

    r = client.delete(f"/api/network-reviews/{rid}")
    assert r.status_code == 204
    assert client.get(f"/api/network-reviews/{rid}").status_code == 404


def test_audit_log_records_create_analyze_delete(client, stub_ollama):
    rid = client.post("/api/network-reviews", json={"name": "phase-1"}).json()["id"]
    client.post(f"/api/network-reviews/{rid}/analyze")
    client.delete(f"/api/network-reviews/{rid}")

    actions = {
        row["action"]
        for row in client.get("/api/audit?limit=50").json()
        if row.get("resource_type") == "network_review"
    }
    assert {
        "network_review.create",
        "network_review.analyze_triggered",
        "network_review.delete",
    } <= actions
