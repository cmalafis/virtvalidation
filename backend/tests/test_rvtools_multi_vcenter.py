"""Tests for the auto-link RVTools upload flow.

Covers four scenarios from the feature spec:

  1. Single-vCenter file → all VMs route to that vCenter.
  2. Multi-vCenter file where every hostname matches a registered
     source.
  3. Multi-vCenter file where one hostname doesn't match anything
     and the operator didn't supply a default (partial-skip).
  4. Multi-vCenter file with a default_vcenter_id fallback covering
     the unmatched bucket.

Plus matching-strategy unit tests for the auto-match endpoint
(exact + fuzzy + miss).
"""

from __future__ import annotations


def _register_vcenter(client, name: str, hostname: str | None = None) -> int:
    r = client.post(
        "/api/sources/vcenters",
        json={"name": name, "hostname": hostname or f"{name}.corp.local"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------------------
# Auto-match endpoint
# ---------------------------------------------------------------------------
def test_auto_match_exact_hostname(client):
    vc_id = _register_vcenter(client, "vc-east-01", "vc-east-01.corp.local")
    r = client.post(
        "/api/sources/vcenters/auto-match",
        json={"hostnames": ["vc-east-01.corp.local"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["matches"]) == 1
    assert body["matches"][0]["matched_vcenter_id"] == vc_id
    assert body["matches"][0]["confidence"] == "exact"
    assert body["unmatched"] == []


def test_auto_match_handles_trailing_dot_and_case(client):
    vc_id = _register_vcenter(client, "vc-east-02", "vc-east-02.corp.local")
    r = client.post(
        "/api/sources/vcenters/auto-match",
        json={"hostnames": ["VC-East-02.CORP.LOCAL."]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["matches"][0]["matched_vcenter_id"] == vc_id
    assert body["matches"][0]["confidence"] == "exact"


def test_auto_match_fuzzy_match_on_short_vs_fqdn(client):
    """Operator registered the short hostname; RVTools contains the
    FQDN. Fuzzy match should pair them."""
    vc_id = _register_vcenter(client, "vc-short", "vc-short")
    r = client.post(
        "/api/sources/vcenters/auto-match",
        json={"hostnames": ["vc-short.corp.local"]},
    )
    body = r.json()
    assert body["matches"][0]["matched_vcenter_id"] == vc_id
    assert body["matches"][0]["confidence"] == "fuzzy"


def test_auto_match_unmatched_returned_separately(client):
    _register_vcenter(client, "vc-east-03", "vc-east-03.corp.local")
    r = client.post(
        "/api/sources/vcenters/auto-match",
        json={"hostnames": ["vc-east-03.corp.local", "vc-unknown.example.com"]},
    )
    body = r.json()
    matched_hosts = [m["hostname"] for m in body["matches"]]
    assert "vc-east-03.corp.local" in matched_hosts
    assert body["unmatched"] == ["vc-unknown.example.com"]


def test_auto_match_rejects_non_list_payload(client):
    r = client.post(
        "/api/sources/vcenters/auto-match",
        json={"hostnames": "not-a-list"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Multi-vCenter import — single vCenter
# ---------------------------------------------------------------------------
def test_single_vcenter_file_routes_all_vms(client):
    vc_id = _register_vcenter(client, "vc-solo", "vc-solo.corp.local")
    payload = {
        "vms": [
            {
                "name": f"vm-{i:03d}",
                "source_hostname": f"vm-{i:03d}.corp",
                "source_vcenter_hostname": "vc-solo.corp.local",
            }
            for i in range(1, 11)
        ],
        "vcenter_mapping": {"vc-solo.corp.local": vc_id},
    }
    r = client.post("/api/rvtools/upload-multi-vcenter", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["imported_per_vcenter"]) == 1
    assert body["imported_per_vcenter"][0]["created"] == 10
    assert body["skipped"] == []
    assert body["errors"] == []
    # vCenter row's vm_count picks up the new VMs.
    assert client.get(f"/api/sources/vcenters/{vc_id}").json()["vm_count"] == 10


# ---------------------------------------------------------------------------
# Multi-vCenter import — all matched
# ---------------------------------------------------------------------------
def test_multi_vcenter_file_routes_per_hostname(client):
    vc_a = _register_vcenter(client, "vc-a", "vc-a.corp.local")
    vc_b = _register_vcenter(client, "vc-b", "vc-b.corp.local")
    payload = {
        "vms": [
            {
                "name": "alpha-1",
                "source_hostname": "a1.corp",
                "source_vcenter_hostname": "vc-a.corp.local",
            },
            {
                "name": "alpha-2",
                "source_hostname": "a2.corp",
                "source_vcenter_hostname": "vc-a.corp.local",
            },
            {
                "name": "beta-1",
                "source_hostname": "b1.corp",
                "source_vcenter_hostname": "vc-b.corp.local",
            },
        ],
        "vcenter_mapping": {
            "vc-a.corp.local": vc_a,
            "vc-b.corp.local": vc_b,
        },
    }
    r = client.post("/api/rvtools/upload-multi-vcenter", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    per_vc = {p["vcenter_id"]: p for p in body["imported_per_vcenter"]}
    assert per_vc[vc_a]["created"] == 2
    assert per_vc[vc_b]["created"] == 1
    # VMs are correctly scoped.
    vms = {vm["name"]: vm for vm in client.get("/api/vms").json()["items"]}
    assert vms["alpha-1"]["source_vcenter_id"] == vc_a
    assert vms["beta-1"]["source_vcenter_id"] == vc_b


# ---------------------------------------------------------------------------
# Multi-vCenter import — unmapped hostname falls into skipped
# ---------------------------------------------------------------------------
def test_unmapped_hostname_skips_those_vms_with_reason(client):
    vc_a = _register_vcenter(client, "vc-mapped", "vc-mapped.corp.local")
    payload = {
        "vms": [
            {
                "name": "mapped-1",
                "source_hostname": "m1.corp",
                "source_vcenter_hostname": "vc-mapped.corp.local",
            },
            {
                "name": "unmapped-1",
                "source_hostname": "u1.corp",
                "source_vcenter_hostname": "vc-unknown.corp.local",
            },
            {
                "name": "unmapped-2",
                "source_hostname": "u2.corp",
                "source_vcenter_hostname": "vc-unknown.corp.local",
            },
        ],
        "vcenter_mapping": {"vc-mapped.corp.local": vc_a},
    }
    r = client.post("/api/rvtools/upload-multi-vcenter", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["imported_per_vcenter"][0]["created"] == 1
    assert len(body["skipped"]) == 2
    # Per-row skip reason names the hostname so the UI can prompt.
    skipped_hosts = {s["detected_hostname"] for s in body["skipped"]}
    assert skipped_hosts == {"vc-unknown.corp.local"}
    assert any("no vcenter mapping" in s["reason"] for s in body["skipped"])


# ---------------------------------------------------------------------------
# Multi-vCenter import — default_vcenter_id fallback
# ---------------------------------------------------------------------------
def test_default_vcenter_id_catches_unmapped_vms(client):
    vc_a = _register_vcenter(client, "vc-mapped", "vc-mapped.corp.local")
    vc_default = _register_vcenter(client, "vc-default", "vc-default.corp.local")
    payload = {
        "vms": [
            {
                "name": "mapped-x",
                "source_hostname": "mx.corp",
                "source_vcenter_hostname": "vc-mapped.corp.local",
            },
            {
                "name": "fallback-x",
                "source_hostname": "fx.corp",
                "source_vcenter_hostname": "vc-other.corp.local",
            },
        ],
        "vcenter_mapping": {"vc-mapped.corp.local": vc_a},
        "default_vcenter_id": vc_default,
    }
    r = client.post("/api/rvtools/upload-multi-vcenter", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    per_vc = {p["vcenter_id"]: p for p in body["imported_per_vcenter"]}
    assert per_vc[vc_a]["created"] == 1
    assert per_vc[vc_default]["created"] == 1
    assert body["skipped"] == []


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------
def test_mapping_to_unknown_vcenter_id_409s(client):
    payload = {
        "vms": [
            {
                "name": "x",
                "source_hostname": "x.corp",
                "source_vcenter_hostname": "vc-unknown.corp.local",
            }
        ],
        "vcenter_mapping": {"vc-unknown.corp.local": 99_999},
    }
    r = client.post("/api/rvtools/upload-multi-vcenter", json=payload)
    assert r.status_code == 409
    assert "99999" in r.json()["detail"]


def test_empty_vm_list_422s(client):
    r = client.post(
        "/api/rvtools/upload-multi-vcenter",
        json={"vms": [], "vcenter_mapping": {}},
    )
    assert r.status_code == 422


def test_no_routable_vms_returns_422(client):
    """Every VM is unmapped and there's no default. Endpoint should
    422 with a clear message rather than silently succeeding."""
    payload = {
        "vms": [
            {
                "name": "ghost",
                "source_hostname": "ghost.corp",
                "source_vcenter_hostname": "vc-nowhere.corp.local",
            }
        ],
        "vcenter_mapping": {},
    }
    r = client.post("/api/rvtools/upload-multi-vcenter", json=payload)
    assert r.status_code == 422
    assert "route" in r.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Re-upload idempotency — second pass should be all updates/unchanged
# ---------------------------------------------------------------------------
def test_re_upload_is_idempotent(client):
    vc = _register_vcenter(client, "vc-idem", "vc-idem.corp.local")
    payload = {
        "vms": [
            {
                "name": "vm-idem",
                "source_hostname": "i.corp",
                "ip_address": "10.0.0.5",
                "source_vcenter_hostname": "vc-idem.corp.local",
            }
        ],
        "vcenter_mapping": {"vc-idem.corp.local": vc},
    }
    first = client.post("/api/rvtools/upload-multi-vcenter", json=payload).json()
    second = client.post("/api/rvtools/upload-multi-vcenter", json=payload).json()
    assert first["imported_per_vcenter"][0]["created"] == 1
    assert second["imported_per_vcenter"][0]["created"] == 0
    assert second["imported_per_vcenter"][0]["unchanged"] == 1
