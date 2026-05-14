"""Tests for the vCenter source registry + RVTools delta preview.

The RVTools commit path rides on the existing /api/vms/bulk endpoint
(VMs submitted with source_vcenter_id), so we test the preview surface
end-to-end and trust the bulk endpoint's existing tests for the commit.
"""

from __future__ import annotations


def _create_vcenter(client, **overrides) -> dict:
    payload = {
        "name": "vc-east",
        "hostname": "vc-east.corp.local",
        "region": "us-east",
        "site": "dc-iad",
        **overrides,
    }
    r = client.post("/api/sources/vcenters", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _enroll(client, name: str, *, source_vcenter_id: int | None = None, **overrides) -> dict:
    body = {
        "name": name,
        "source_hostname": f"{name}.corp.local",
        "ip_address": "10.0.0.5",
        "role": "database",
        **overrides,
    }
    if source_vcenter_id is not None:
        body["source_vcenter_id"] = source_vcenter_id
    r = client.post("/api/vms", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------
def test_list_vcenters_empty_by_default(client):
    r = client.get("/api/sources/vcenters")
    assert r.status_code == 200
    assert r.json() == []


def test_create_vcenter_returns_201_with_defaults(client):
    body = _create_vcenter(client)
    assert body["name"] == "vc-east"
    assert body["classification_level"] == "unclassified"
    assert body["status"] == "active"
    assert body["vm_count"] == 0


def test_create_vcenter_rejects_duplicate_name(client):
    _create_vcenter(client)
    r = client.post(
        "/api/sources/vcenters",
        json={"name": "vc-east", "hostname": "another.host"},
    )
    assert r.status_code == 409


def test_get_vcenter_includes_vm_count(client):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])
    _enroll(client, "vm-b", source_vcenter_id=vc["id"])
    body = client.get(f"/api/sources/vcenters/{vc['id']}").json()
    assert body["vm_count"] == 2


def test_list_vcenters_aggregates_counts(client):
    a = _create_vcenter(client, name="vc-a", hostname="a.host")
    b = _create_vcenter(client, name="vc-b", hostname="b.host")
    _enroll(client, "vm-1", source_vcenter_id=a["id"])
    _enroll(client, "vm-2", source_vcenter_id=a["id"])
    _enroll(client, "vm-3", source_vcenter_id=b["id"])

    listing = client.get("/api/sources/vcenters").json()
    counts = {row["name"]: row["vm_count"] for row in listing}
    assert counts == {"vc-a": 2, "vc-b": 1}


def test_patch_vcenter_updates_fields(client):
    vc = _create_vcenter(client)
    r = client.patch(
        f"/api/sources/vcenters/{vc['id']}",
        json={"classification_level": "cui", "site": "dc-bos"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["classification_level"] == "cui"
    assert body["site"] == "dc-bos"


def test_delete_vcenter_unassigns_vms_via_set_null(client):
    """Deleting a vCenter must NOT cascade-delete its VMs — federal
    operators want a deliberate two-step purge, not a silent fleet wipe.
    The VM rows survive with source_vcenter_id flipped to NULL."""
    vc = _create_vcenter(client)
    vm = _enroll(client, "survivor", source_vcenter_id=vc["id"])

    r = client.delete(f"/api/sources/vcenters/{vc['id']}")
    assert r.status_code == 204

    surviving = client.get(f"/api/vms/{vm['id']}").json()
    assert surviving["id"] == vm["id"]
    assert surviving["source_vcenter_id"] is None


def test_delete_vcenter_audit_records_unassigned_count(client):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])
    _enroll(client, "vm-b", source_vcenter_id=vc["id"])
    client.delete(f"/api/sources/vcenters/{vc['id']}")

    rows = client.get("/api/audit?action=vcenter.delete").json()
    assert len(rows) == 1
    assert rows[0]["details"]["vms_unassigned"] == 2


# ---------------------------------------------------------------------------
# RVTools delta preview
# ---------------------------------------------------------------------------
def _delta(client, vcenter_id: int, vms: list[dict]) -> dict:
    r = client.post(
        f"/api/sources/vcenters/{vcenter_id}/rvtools/preview",
        json={"vms": vms},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_delta_preview_marks_unknown_names_as_new(client):
    vc = _create_vcenter(client)
    body = _delta(client, vc["id"], [{"name": "fresh-1"}, {"name": "fresh-2"}])
    assert {item["name"] for item in body["new"]} == {"fresh-1", "fresh-2"}
    assert body["updated"] == []
    assert body["removed"] == []
    assert body["summary"]["new"] == 2


def test_delta_preview_marks_existing_names_unchanged_when_fields_match(client):
    vc = _create_vcenter(client)
    _enroll(
        client,
        "stable",
        source_vcenter_id=vc["id"],
        role="database",
        environment="prod",
    )
    body = _delta(
        client,
        vc["id"],
        [
            {
                "name": "stable",
                "source_hostname": "stable.corp.local",
                "role": "database",
                "environment": "prod",
                "ip_address": "10.0.0.5",
            }
        ],
    )
    assert body["new"] == []
    assert body["updated"] == []
    assert {item["name"] for item in body["unchanged"]} == {"stable"}


def test_delta_preview_marks_changed_fields_as_updated_with_diff(client):
    vc = _create_vcenter(client)
    _enroll(
        client,
        "drifty",
        source_vcenter_id=vc["id"],
        role="app",
        environment="staging",
    )
    body = _delta(
        client,
        vc["id"],
        [
            {
                "name": "drifty",
                "source_hostname": "drifty.corp.local",
                "ip_address": "10.0.0.5",
                "role": "app",
                "environment": "prod",  # changed
            }
        ],
    )
    assert body["new"] == []
    assert len(body["updated"]) == 1
    item = body["updated"][0]
    assert item["name"] == "drifty"
    assert item["diff"]["environment"]["before"] == "staging"
    assert item["diff"]["environment"]["after"] == "prod"
    # Fields that didn't change must NOT appear in the diff.
    assert "role" not in item["diff"]


def test_delta_preview_marks_missing_vms_as_removed(client):
    vc = _create_vcenter(client)
    _enroll(client, "vm-a", source_vcenter_id=vc["id"])
    _enroll(client, "vm-b", source_vcenter_id=vc["id"])

    body = _delta(client, vc["id"], [{"name": "vm-a", "source_hostname": "vm-a.corp.local"}])
    assert {item["name"] for item in body["removed"]} == {"vm-b"}


def test_delta_preview_ignores_vms_in_other_vcenters(client):
    """Match is scoped to the target vCenter — a VM with the same name
    in another vCenter must not register as a match."""
    vc_a = _create_vcenter(client, name="vc-a", hostname="a")
    vc_b = _create_vcenter(client, name="vc-b", hostname="b")
    _enroll(client, "shared-name", source_vcenter_id=vc_a["id"])

    body = _delta(client, vc_b["id"], [{"name": "shared-name"}])
    # Should be 'new' from vc-b's perspective, even though vc-a has it.
    assert [item["name"] for item in body["new"]] == ["shared-name"]
    assert body["unchanged"] == []


def test_delta_preview_compares_vsphere_lists_order_insensitive(client):
    """Operators sometimes re-export RVTools and the network/datastore
    columns come back in a different order. Treating that as 'updated'
    floods the review queue with non-changes — so the diff engine
    sorts before comparing."""
    vc = _create_vcenter(client)
    _enroll(
        client,
        "vm-net",
        source_vcenter_id=vc["id"],
        vsphere_networks=["VM Network", "DB Backend"],
    )
    body = _delta(
        client,
        vc["id"],
        [
            {
                "name": "vm-net",
                "vsphere_networks": ["DB Backend", "VM Network"],  # reordered
            }
        ],
    )
    assert body["updated"] == []
    assert {item["name"] for item in body["unchanged"]} == {"vm-net"}


def test_delta_preview_404_for_unknown_vcenter(client):
    r = client.post(
        "/api/sources/vcenters/99999/rvtools/preview",
        json={"vms": [{"name": "anything"}]},
    )
    assert r.status_code == 404


def test_delta_preview_summary_counts_match_buckets(client):
    vc = _create_vcenter(client)
    _enroll(client, "stays", source_vcenter_id=vc["id"], role="database")
    _enroll(client, "leaves", source_vcenter_id=vc["id"])

    body = _delta(
        client,
        vc["id"],
        [
            # stays — unchanged (matches enrollment defaults)
            {
                "name": "stays",
                "source_hostname": "stays.corp.local",
                "role": "database",
                "ip_address": "10.0.0.5",
            },
            # new
            {"name": "fresh"},
            # 'leaves' is omitted → removed
        ],
    )
    s = body["summary"]
    assert s["new"] == len(body["new"]) == 1
    assert s["removed"] == len(body["removed"]) == 1
    assert s["unchanged"] == len(body["unchanged"]) == 1
    assert s["total_in_payload"] == 2
    assert s["total_in_vcenter"] == 2
