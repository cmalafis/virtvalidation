"""Migratability assessment: rules, persistence, filters, plan gate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.assessment import VMFacts, assess, blocks_migration_type

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "rvtools-1000.xlsx"
VCENTERS = [f"vc-site{i:02d}.hospital.example" for i in (1, 2, 3)]


def _disk(**over):
    return {
        "label": "Hard disk 1",
        "mode": "persistent",
        "sharing": "sharingNone",
        "shared_bus": "noSharing",
        "raw": False,
        "controller": "SCSI controller 0",
        "datastore": "ds1",
        **over,
    }


def _facts(hardware=None, **over) -> VMFacts:
    base = {
        "name": "web-01",
        "source_hostname": "web-01.example",
        "ip_address": "10.0.0.5",
        "power_state": "poweredOn",
        "guest_os_full": "Red Hat Enterprise Linux 9 (64-bit)",
        "vsphere_datastores": ("ds1",),
        "hardware": {
            "v": 1,
            "cbt": True,
            "cpu_hot_add": False,
            "mem_hot_add": False,
            "disks": [_disk()],
            **(hardware or {}),
        },
    }
    return VMFacts(**{**base, **over})


def _ids(a):
    return {f.id for f in a.findings}


def test_clean_vm_is_ok_and_says_what_it_could_not_check():
    a = assess(_facts())
    assert a.status == "ok" and a.findings == []
    skipped = {s.id for s in a.not_evaluated}
    # RVTools can't answer these — the assessment must say so, not imply "clean".
    assert {"vmware.tpm.detected", "vmware.passthrough_device.detected"} <= skipped


@pytest.mark.parametrize(
    ("hardware", "over", "finding", "status"),
    [
        (
            {"disks": [_disk(raw=True, raw_compat_mode="physicalMode")]},
            {},
            "vmware.disk.rdm.detected",
            "warning",
        ),
        (
            {"disks": [_disk(mode="independent_persistent")]},
            {},
            "vmware.disk_mode.independent",
            "warning",
        ),
        (
            {"disks": [_disk(sharing="sharingMultiWriter")]},
            {},
            "vmware.disk.shared.detected",
            "warning",
        ),
        (
            {"disks": [_disk(shared_bus="physicalSharing")]},
            {},
            "vmware.disk.shared.detected",
            "warning",
        ),
        (
            {"disks": [_disk(controller="NVME controller 0")]},
            {},
            "vv.disk.nvme.detected",
            "blocked",
        ),
        ({"disks": [_disk(datastore=None)]}, {}, "vmware.datastore.missing", "blocked"),
        ({"cbt": False}, {}, "vmware.changed_block_tracking.disabled", "warning"),
        ({"consolidation_needed": True}, {}, "vmware.consolidation_needed", "warning"),
        ({"snapshots": [{"name": "pre-patch"}]}, {}, "vmware.snapshot.detected", "ok"),
        ({"cpu_hot_add": True}, {}, "vmware.cpu_memory.hotplug.enabled", "warning"),
        ({"ft_state": "running"}, {}, "vmware.fault_tolerance.enabled", "ok"),
        ({"cluster_rules": "db-separate"}, {}, "vmware.host_affinity.detected", "warning"),
        ({"secure_boot": True, "firmware": "efi"}, {}, "vv.firmware.secure_boot", "warning"),
        ({}, {"guest_os_full": "Ubuntu Linux (64-bit)"}, "vmware.os.unsupported", "warning"),
        (
            {},
            {"guest_os_full": "Microsoft Windows Server 2012 (64-bit)"},
            "vv.os.windows2012.no_virtio",
            "warning",
        ),
        ({}, {"name": "Payroll DB_01"}, "vmware.vm.name.invalid", "warning"),
        ({}, {"name": "x" * 64}, "vmware.vm.name.invalid", "warning"),
        ({}, {"ip_address": None}, "vmware.vm_missing_ip.detected", "warning"),
        ({}, {"source_hostname": "localhost.localdomain"}, "vmware.hostname.default", "warning"),
    ],
)
def test_rule(hardware, over, finding, status):
    a = assess(_facts(hardware, **over))
    assert finding in _ids(a)
    assert a.status == status
    f = next(x for x in a.findings if x.id == finding)
    assert f.remediation and f.assessment  # every finding tells the operator what to do


def test_supported_guests_match_forklift_list():
    for os_name in (
        "Red Hat Enterprise Linux 7 (64-bit)",
        "Microsoft Windows Server 2025 (64-bit)",
        "Microsoft Windows 11 (64-bit)",
    ):
        assert "vmware.os.unsupported" not in _ids(assess(_facts(guest_os_full=os_name)))


def test_disk_rules_report_not_evaluated_without_vdisk_rows():
    a = assess(_facts({"disks": []}))
    skipped = {s.id: s.reason for s in a.not_evaluated}
    for rule in (
        "vmware.disk.rdm.detected",
        "vmware.disk_mode.independent",
        "vmware.disk.shared.detected",
    ):
        assert "vDisk" in skipped[rule]
    assert not _ids(a) & set(skipped)  # never both flagged and skipped


def test_manual_vm_is_unknown_not_ok():
    a = assess(VMFacts(name="hand-added"))
    assert a.status == "unknown" and a.findings == []


def test_cbt_only_blocks_warm():
    doc = assess(_facts({"cbt": False})).to_dict()
    assert blocks_migration_type(doc, "cold") == []
    assert blocks_migration_type(doc, "warm") == ["vmware.changed_block_tracking.disabled"]
    nvme = assess(_facts({"disks": [_disk(controller="NVME controller 0")]})).to_dict()
    assert blocks_migration_type(nvme, "cold") == ["vv.disk.nvme.detected"]


# --------------------------------------------------------------------------
# Through the API on the 1,000-VM fixture
# --------------------------------------------------------------------------
@pytest.fixture
def fleet(client, tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_SPOOL_DIR", str(tmp_path / "spool"))
    vc = {
        h: client.post(
            "/api/sources/vcenters", json={"name": h.split(".")[0], "hostname": h}
        ).json()["id"]
        for h in VCENTERS
    }
    r = client.post(
        "/api/imports/rvtools",
        files={"file": (FIXTURE.name, FIXTURE.read_bytes(), "application/octet-stream")},
        data={"vcenter_mapping": json.dumps(vc)},
    )
    assert client.get(f"/api/imports/{r.json()['id']}").json()["status"] == "completed"
    return client


def test_import_assesses_every_vm_and_summary_matches_the_fixture(fleet):
    s = fleet.get("/api/assessment/summary").json()
    assert s["total"] == 1000 and s["by_status"]["unknown"] == 0
    by_id = {f["id"]: f for f in s["findings"]}
    # counts verified directly against the fixture's vDisk sheet
    assert by_id["vmware.disk.rdm.detected"]["vm_count"] == 13
    assert by_id["vmware.disk.shared.detected"]["vm_count"] == 11
    assert (
        by_id["vmware.disk_mode.independent"]["vm_count"] == 22
    )  # includes the 13 RDMs, which are independent too
    assert by_id["vmware.changed_block_tracking.disabled"]["applies_to"] == "warm"
    assert by_id["vmware.datastore.missing"]["category"] == "Critical"
    assert s["by_status"]["blocked"] == 1  # orphan-no-datastore-001
    assert any(
        n["id"] == "vmware.tpm.detected" and n["vm_count"] == 1000 for n in s["not_evaluated"]
    )


def test_inventory_filters_by_status_and_finding(fleet):
    blocked = fleet.get("/api/vms?assessment_status=blocked").json()
    assert [v["name"] for v in blocked["items"]] == ["orphan-no-datastore-001"]
    assert blocked["items"][0]["assessment_findings"][0]["id"] == "vmware.datastore.missing"
    assert "assessment" not in blocked["items"][0]  # full document is per-VM only

    rdm = fleet.get("/api/vms?assessment_finding=vmware.disk.rdm.detected&limit=100").json()
    assert rdm["total"] == 13
    facets = fleet.get("/api/vms/facets").json()["assessment_status"]
    assert facets["blocked"] == 1 and sum(facets.values()) == 1000

    detail = fleet.get(f"/api/vms/{rdm['items'][0]['id']}/assessment").json()
    f = next(x for x in detail["findings"] if x["id"] == "vmware.disk.rdm.detected")
    assert f["evidence"]["disks"] and "rdmAsLun" in f["remediation"]
    assert detail["not_evaluated"]


def test_rerun_is_idempotent(fleet):
    assert fleet.post("/api/assessment/run").json() == {
        "ruleset": "forklift-2.12.1+vv1",
        "assessed": 1000,
        "changed": 0,
    }


def test_plan_gate_refuses_blocked_and_warm_unfit_vms(fleet, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.llm_backend_type", "mock")
    blocked = fleet.get("/api/vms?assessment_status=blocked").json()["items"][0]
    r = fleet.post("/api/plans", json={"vm_ids": [blocked["id"]]})
    assert r.status_code == 422
    assert "orphan-no-datastore-001 (vmware.datastore.missing)" in r.json()["detail"]

    no_cbt = fleet.get(
        "/api/vms?assessment_finding=vmware.changed_block_tracking.disabled&assessment_status=warning&limit=1"
    ).json()["items"][0]
    warm = fleet.post("/api/plans", json={"vm_ids": [no_cbt["id"]], "migration_type": "warm"})
    assert warm.status_code == 422 and "cold plan" in warm.json()["detail"]
    # The same VM is fine cold — the gate must not be what stops it (no mapping
    # exists in this test, so it fails later, on target resolution).
    cold = fleet.post("/api/plans", json={"vm_ids": [no_cbt["id"]]})
    assert "cannot migrate as a" not in json.dumps(cold.json())
