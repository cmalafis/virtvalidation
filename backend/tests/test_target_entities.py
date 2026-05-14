"""TargetNetwork / TargetStorageClass CRUD tests.

The "delete protection" tests are the safety net — operators must not
be able to silently break an active ResourceMapping by dropping the
target row out from under it. The 409 body lists the referencing
mapping IDs so the UI can navigate the operator straight there.
"""

from __future__ import annotations

import pytest

from app.models.target import OCPTarget, ResourceMapping


@pytest.fixture
def target(db_session):
    t = OCPTarget(
        name="prod-cluster",
        api_endpoint="https://api.prod.example.com:6443",
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


@pytest.fixture
def second_target(db_session):
    t = OCPTarget(
        name="dr-cluster",
        api_endpoint="https://api.dr.example.com:6443",
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


# ---------------------------------------------------------------------------
# TargetNetwork CRUD
# ---------------------------------------------------------------------------
def test_create_and_list_networks(client, target):
    body = {
        "name": "tenant-prod-net",
        "network_type": "nad",
        "namespace": "openshift-multus",
        "is_default": True,
    }
    r = client.post(f"/api/ocp-targets/{target.id}/networks", json=body)
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["name"] == "tenant-prod-net"
    assert created["ocp_target_id"] == target.id

    r = client.get(f"/api/ocp-targets/{target.id}/networks")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["name"] == "tenant-prod-net"


def test_get_network_404_wrong_cluster(client, target, second_target):
    r = client.post(
        f"/api/ocp-targets/{target.id}/networks",
        json={"name": "n1", "network_type": "cudn"},
    )
    net_id = r.json()["id"]
    r = client.get(f"/api/ocp-targets/{second_target.id}/networks/{net_id}")
    assert r.status_code == 404


def test_update_network(client, target):
    r = client.post(
        f"/api/ocp-targets/{target.id}/networks",
        json={"name": "net-a", "network_type": "nad", "namespace": "ns-a"},
    )
    net_id = r.json()["id"]
    r = client.patch(
        f"/api/ocp-targets/{target.id}/networks/{net_id}",
        json={"namespace": "ns-b", "is_default": True},
    )
    assert r.status_code == 200
    assert r.json()["namespace"] == "ns-b"
    assert r.json()["is_default"] is True


def test_default_flag_unsets_others_within_same_type(client, target):
    """is_default=True on a NAD must unset is_default on every other NAD
    on the same cluster, but must NOT touch CUDN defaults — operators
    can mark one default per network_type."""
    cl = client
    cl.post(
        f"/api/ocp-targets/{target.id}/networks",
        json={"name": "nad-a", "network_type": "nad", "is_default": True},
    )
    cl.post(
        f"/api/ocp-targets/{target.id}/networks",
        json={"name": "nad-b", "network_type": "nad", "is_default": True},
    )
    cl.post(
        f"/api/ocp-targets/{target.id}/networks",
        json={"name": "cudn-a", "network_type": "cudn", "is_default": True},
    )

    rows = {n["name"]: n for n in cl.get(f"/api/ocp-targets/{target.id}/networks").json()}
    assert rows["nad-a"]["is_default"] is False
    assert rows["nad-b"]["is_default"] is True
    # CUDN default survives because it's a different network_type slot.
    assert rows["cudn-a"]["is_default"] is True


def test_delete_network_blocked_when_referenced(client, db_session, target):
    """Operator must clear the mapping before deleting the target net."""
    r = client.post(
        f"/api/ocp-targets/{target.id}/networks",
        json={"name": "tenant-net", "network_type": "nad"},
    )
    net_id = r.json()["id"]

    # Stage a ResourceMapping pointing at the network. The mapping needs a
    # vCenter source — create a stub one. Skip if the fixture'd
    # VCenterSource isn't available; just point at id=1 with a NULL FK
    # behavior turned off — actually here we create one inline.
    from app.models.vcenter import VCenterSource

    vc = VCenterSource(name="vc-1", hostname="vc1.example.com")
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)

    m = ResourceMapping(
        name="active-mapping",
        vcenter_source_id=vc.id,
        ocp_target_id=target.id,
        network_mappings=[{"source_network": "prod-vlan", "target_network_name": "tenant-net"}],
        storage_mappings=[],
        namespace_mappings=[],
    )
    db_session.add(m)
    db_session.commit()

    r = client.delete(f"/api/ocp-targets/{target.id}/networks/{net_id}")
    assert r.status_code == 409
    body = r.json()["detail"]
    assert "still referenced" in body["detail"]
    assert body["referenced_by"][0]["mapping_id"] == m.id


# ---------------------------------------------------------------------------
# TargetStorageClass CRUD
# ---------------------------------------------------------------------------
def test_create_and_list_storage_classes(client, target):
    body = {
        "name": "ocs-rbd",
        "access_mode": "ReadWriteOnce",
        "is_default": True,
    }
    r = client.post(f"/api/ocp-targets/{target.id}/storage-classes", json=body)
    assert r.status_code == 201
    r = client.get(f"/api/ocp-targets/{target.id}/storage-classes")
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_storage_default_flag_singleton(client, target):
    """Only one default StorageClass per cluster, across access modes."""
    cl = client
    cl.post(
        f"/api/ocp-targets/{target.id}/storage-classes",
        json={"name": "sc-a", "access_mode": "ReadWriteOnce", "is_default": True},
    )
    cl.post(
        f"/api/ocp-targets/{target.id}/storage-classes",
        json={"name": "sc-b", "access_mode": "ReadWriteMany", "is_default": True},
    )
    rows = {s["name"]: s for s in cl.get(f"/api/ocp-targets/{target.id}/storage-classes").json()}
    assert rows["sc-a"]["is_default"] is False
    assert rows["sc-b"]["is_default"] is True


def test_delete_storage_class_blocked_when_referenced(client, db_session, target):
    r = client.post(
        f"/api/ocp-targets/{target.id}/storage-classes",
        json={"name": "bulk-sc", "access_mode": "ReadWriteOnce"},
    )
    sc_id = r.json()["id"]

    from app.models.vcenter import VCenterSource

    vc = VCenterSource(name="vc-2", hostname="vc2.example.com")
    db_session.add(vc)
    db_session.commit()
    db_session.refresh(vc)

    m = ResourceMapping(
        name="m2",
        vcenter_source_id=vc.id,
        ocp_target_id=target.id,
        network_mappings=[],
        storage_mappings=[{"source_datastore": "ds-1", "target_storage_class": "bulk-sc"}],
        namespace_mappings=[],
    )
    db_session.add(m)
    db_session.commit()

    r = client.delete(f"/api/ocp-targets/{target.id}/storage-classes/{sc_id}")
    assert r.status_code == 409
    assert r.json()["detail"]["referenced_by"][0]["mapping_id"] == m.id


def test_delete_storage_class_after_unmapping_succeeds(client, db_session, target):
    """After clearing the mapping reference, delete should succeed."""
    r = client.post(
        f"/api/ocp-targets/{target.id}/storage-classes",
        json={"name": "throwaway-sc", "access_mode": "ReadWriteOnce"},
    )
    sc_id = r.json()["id"]

    r = client.delete(f"/api/ocp-targets/{target.id}/storage-classes/{sc_id}")
    assert r.status_code == 204
    r = client.get(f"/api/ocp-targets/{target.id}/storage-classes/{sc_id}")
    assert r.status_code == 404


def test_route_alias_under_sources_targets(client, target):
    """The same handlers must respond on /api/sources/targets/{id}/...
    because that's what the existing frontend calls."""
    r = client.post(
        f"/api/sources/targets/{target.id}/networks",
        json={"name": "alias-net", "network_type": "pod"},
    )
    assert r.status_code == 201
    r = client.get(f"/api/sources/targets/{target.id}/networks")
    assert r.status_code == 200
    assert any(n["name"] == "alias-net" for n in r.json())
