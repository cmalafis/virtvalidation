"""Integration tests for the FastAPI layer via TestClient.

The backend mounts the VM router at `/vms` (the frontend's `/api/vms` is
mapped by the Vite proxy). These tests hit the internal path directly.
"""

from __future__ import annotations


def test_health_endpoint_returns_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "version": "0.1.0"}


def test_list_vms_empty_by_default(client):
    r = client.get("/vms")
    assert r.status_code == 200
    assert r.json() == []


def test_create_vm_persists_and_returns_201(client, mock_vm_payload):
    r = client.post("/vms", json=mock_vm_payload)
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "db-01"
    assert body["ip_address"] == "10.0.0.5"
    assert body["role"] == "database"
    assert body["status"] == "discovered"
    assert "id" in body
    assert "created_at" in body


def test_list_vms_returns_created_vm(client, mock_vm_payload):
    client.post("/vms", json=mock_vm_payload)
    r = client.get("/vms")
    assert r.status_code == 200
    vms = r.json()
    assert len(vms) == 1
    assert vms[0]["name"] == "db-01"


def test_create_duplicate_vm_returns_409(client, mock_vm_payload):
    r1 = client.post("/vms", json=mock_vm_payload)
    assert r1.status_code == 201
    r2 = client.post("/vms", json=mock_vm_payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_create_vm_validates_required_fields(client):
    r = client.post("/vms", json={"ip_address": "10.0.0.5"})
    assert r.status_code == 422


def test_get_vm_by_id(client, mock_vm_payload):
    created = client.post("/vms", json=mock_vm_payload).json()
    r = client.get(f"/vms/{created['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]
    assert r.json()["name"] == "db-01"


def test_get_vm_not_found_returns_404(client):
    r = client.get("/vms/99999")
    assert r.status_code == 404


def test_list_vms_filters_by_status(client, mock_vm_payload):
    client.post("/vms", json=mock_vm_payload)
    r = client.get("/vms?status=discovered")
    assert r.status_code == 200
    assert len(r.json()) == 1
    r = client.get("/vms?status=validated")
    assert r.status_code == 200
    assert r.json() == []


def test_list_vms_respects_limit(client):
    for i in range(5):
        client.post(
            "/vms",
            json={"name": f"vm-{i}", "source_hostname": f"vm-{i}.local"},
        )
    r = client.get("/vms?limit=3")
    assert r.status_code == 200
    assert len(r.json()) == 3


def test_bulk_create_persists_and_reports_dupes(client, mock_vm_payload):
    # Seed an existing VM that the bulk request will collide with.
    client.post("/vms", json=mock_vm_payload)

    r = client.post(
        "/vms/bulk",
        json={
            "vms": [
                {"name": "db-01", "source_hostname": "db-01.vmware.local"},  # collision
                {"name": "app-01", "source_hostname": "app-01.local", "ssh_user": "ec2-user"},
                {"name": "app-02", "source_hostname": "app-02.local"},
                {"name": "app-01", "source_hostname": "app-01.local"},  # within-batch dup
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    assert len(body["created"]) == 2
    created_names = sorted(vm["name"] for vm in body["created"])
    assert created_names == ["app-01", "app-02"]

    skipped = body["skipped"]
    assert len(skipped) == 2
    skipped_by_name = {s["name"]: s["reason"] for s in skipped}
    assert "already enrolled" in skipped_by_name["db-01"]
    assert "batch" in skipped_by_name["app-01"]

    # The two new VMs landed in the DB
    listing = client.get("/vms").json()
    assert {v["name"] for v in listing} == {"db-01", "app-01", "app-02"}

    # ssh_user round-trips
    app01 = next(v for v in listing if v["name"] == "app-01")
    assert app01["ssh_user"] == "ec2-user"


def test_bulk_create_validates_entries(client):
    r = client.post("/vms/bulk", json={"vms": []})
    assert r.status_code == 422


def test_create_vm_accepts_ssh_user(client):
    r = client.post(
        "/vms",
        json={
            "name": "db-99",
            "source_hostname": "db-99.local",
            "ssh_user": "rocky",
        },
    )
    assert r.status_code == 201
    assert r.json()["ssh_user"] == "rocky"
