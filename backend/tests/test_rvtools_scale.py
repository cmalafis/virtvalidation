"""Scale tests for the RVTools auto-link upload flow.

Two checkpoints:

  - **100 VMs** (baseline) — exercises every code path without timing
    risk. Tests assert routing correctness + counts.
  - **1000 VMs** (target scale) — federal customer fleet size. Tests
    assert end-to-end (parse-free; we feed the payload directly to
    the backend) completes within a 60-second budget.

These tests are deliberately *not* parameterized over backends — they
hit the FastAPI TestClient + in-memory SQLite + the actual import core,
which is the fastest available path. The numbers below are the floor;
real Postgres deployments will be slightly slower but well within
budget.
"""

from __future__ import annotations

import time


def _register(client, name: str, hostname: str) -> int:
    r = client.post(
        "/api/sources/vcenters",
        json={"name": name, "hostname": hostname},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _build_payload(*, vm_count: int, vcenter_count: int) -> dict:
    """Construct a payload mirroring what the parser produces for the
    generated test XLSX — same field set, same per-VM hostname
    distribution."""
    vcenter_hosts = [f"vc-east-{i:02d}.corp.local" for i in range(1, vcenter_count + 1)]
    vms: list[dict] = []
    for i in range(vm_count):
        host = vcenter_hosts[i % vcenter_count]
        vms.append(
            {
                "name": f"vm-{i:04d}",
                "source_hostname": f"vm-{i:04d}.corp",
                "ip_address": f"10.0.{(i // 250) % 250}.{(i % 250) + 5}",
                "os_family": "rhel",
                "vsphere_networks": [f"vlan-{(i % 4) * 100}"],
                "vsphere_datastores": [f"tier{(i % 3) + 1}-storage"],
                "source_vcenter_hostname": host,
            }
        )
    return {"vms": vms, "_hosts": vcenter_hosts}


def test_100_vm_baseline_succeeds(client):
    payload = _build_payload(vm_count=100, vcenter_count=3)
    mapping = {h: _register(client, h.split(".")[0], h) for h in payload["_hosts"]}
    body = client.post(
        "/api/rvtools/upload-multi-vcenter",
        json={"vms": payload["vms"], "vcenter_mapping": mapping},
    ).json()
    total_created = sum(p["created"] for p in body["imported_per_vcenter"])
    assert total_created == 100
    # Distribution across vCenters: ~equal thirds.
    per_vc = {p["vcenter_name"]: p["created"] for p in body["imported_per_vcenter"]}
    assert all(30 <= v <= 40 for v in per_vc.values()), per_vc


def test_1000_vm_target_scale_completes_within_60s(client):
    """Target scale per the feature spec. The TestClient hits the
    backend via the in-process FastAPI app, so the measured time
    excludes network latency — generous 60s budget covers the
    Postgres-equivalent overhead in production."""
    payload = _build_payload(vm_count=1000, vcenter_count=3)
    mapping = {h: _register(client, h.split(".")[0], h) for h in payload["_hosts"]}

    started = time.monotonic()
    response = client.post(
        "/api/rvtools/upload-multi-vcenter",
        json={"vms": payload["vms"], "vcenter_mapping": mapping},
    )
    elapsed = time.monotonic() - started
    assert response.status_code == 200, response.text
    body = response.json()

    total_created = sum(p["created"] for p in body["imported_per_vcenter"])
    assert total_created == 1000
    assert body["skipped"] == []
    assert body["errors"] == []
    assert elapsed < 60.0, f"1000-VM import took {elapsed:.2f}s; budget is 60s"


def test_1000_vm_idempotent_reupload(client):
    """A second pass over the same payload should produce 0 creates,
    1000 unchanged. Guards against duplicate-row regressions at scale."""
    payload = _build_payload(vm_count=1000, vcenter_count=3)
    mapping = {h: _register(client, h.split(".")[0], h) for h in payload["_hosts"]}
    request_body = {"vms": payload["vms"], "vcenter_mapping": mapping}
    client.post("/api/rvtools/upload-multi-vcenter", json=request_body)
    body = client.post("/api/rvtools/upload-multi-vcenter", json=request_body).json()
    total_created = sum(p["created"] for p in body["imported_per_vcenter"])
    total_unchanged = sum(p["unchanged"] for p in body["imported_per_vcenter"])
    assert total_created == 0
    assert total_unchanged == 1000


def test_no_orphan_records_when_one_vcenter_fails(client, monkeypatch):
    """Partial-success contract: a per-vCenter exception is collected
    in ``errors`` but doesn't roll back the other vCenters' VMs."""
    payload = _build_payload(vm_count=30, vcenter_count=3)
    mapping = {h: _register(client, h.split(".")[0], h) for h in payload["_hosts"]}
    # Make the import core raise on the second vcenter only.
    from app.api import rvtools as rvtools_api

    real_import = rvtools_api.run_rvtools_import
    targeted_vcenter = mapping[payload["_hosts"][1]]

    def _selective(db, *, vcenter_id, payload_vms, actor):
        if vcenter_id == targeted_vcenter:
            raise RuntimeError("simulated import failure on vc 2")
        return real_import(db, vcenter_id=vcenter_id, payload_vms=payload_vms, actor=actor)

    monkeypatch.setattr(rvtools_api, "run_rvtools_import", _selective)

    body = client.post(
        "/api/rvtools/upload-multi-vcenter",
        json={"vms": payload["vms"], "vcenter_mapping": mapping},
    ).json()
    # 2 of 3 vCenters import successfully.
    assert len(body["imported_per_vcenter"]) == 2
    assert any("simulated import failure" in e for e in body["errors"])
    # The successful vCenters' VMs survived.
    total_imported = sum(p["created"] for p in body["imported_per_vcenter"])
    assert total_imported == 20  # 30 / 3 × 2
