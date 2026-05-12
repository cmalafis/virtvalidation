"""Integration tests for the FastAPI layer via TestClient.

All HTTP routes are mounted under `/api/<resource>` so the backend speaks
the same prefix the frontend (and the nginx + vite proxies) request.
"""

from __future__ import annotations


def test_health_endpoint_returns_ok(client):
    r = client.get("/api/health/full")
    assert r.status_code == 200
    body = r.json()
    assert body["api"]["status"] == "online"
    assert body["api"]["version"] == "0.1.0"


def test_list_vms_empty_by_default(client):
    r = client.get("/api/vms")
    assert r.status_code == 200
    body = r.json()
    assert body["items"] == []
    assert body["total"] == 0
    assert body["skip"] == 0
    assert body["limit"] == 50  # DEFAULT_PAGE_SIZE


def test_create_vm_persists_and_returns_201(client, mock_vm_payload):
    r = client.post("/api/vms", json=mock_vm_payload)
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "db-01"
    assert body["ip_address"] == "10.0.0.5"
    assert body["role"] == "database"
    assert body["status"] == "discovered"
    assert "id" in body
    assert "created_at" in body


def test_list_vms_returns_created_vm(client, mock_vm_payload):
    client.post("/api/vms", json=mock_vm_payload)
    r = client.get("/api/vms")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["name"] == "db-01"


def test_create_duplicate_vm_returns_409(client, mock_vm_payload):
    r1 = client.post("/api/vms", json=mock_vm_payload)
    assert r1.status_code == 201
    r2 = client.post("/api/vms", json=mock_vm_payload)
    assert r2.status_code == 409
    assert "already exists" in r2.json()["detail"]


def test_create_vm_validates_required_fields(client):
    r = client.post("/api/vms", json={"ip_address": "10.0.0.5"})
    assert r.status_code == 422


def test_get_vm_by_id(client, mock_vm_payload):
    created = client.post("/api/vms", json=mock_vm_payload).json()
    r = client.get(f"/api/vms/{created['id']}")
    assert r.status_code == 200
    assert r.json()["id"] == created["id"]
    assert r.json()["name"] == "db-01"


def test_get_vm_not_found_returns_404(client):
    r = client.get("/api/vms/99999")
    assert r.status_code == 404


def test_list_vms_filters_by_status(client, mock_vm_payload):
    client.post("/api/vms", json=mock_vm_payload)
    r = client.get("/api/vms?status=discovered")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    r = client.get("/api/vms?status=validated")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_vms_respects_limit(client):
    for i in range(5):
        client.post(
            "/api/vms",
            json={"name": f"vm-{i}", "source_hostname": f"vm-{i}.local"},
        )
    r = client.get("/api/vms?limit=3")
    assert r.status_code == 200
    body = r.json()
    # total reflects pre-pagination count; items is the clipped page.
    assert body["total"] == 5
    assert len(body["items"]) == 3
    assert body["limit"] == 3


def test_bulk_create_persists_and_reports_dupes(client, mock_vm_payload):
    # Seed an existing VM that the bulk request will collide with.
    client.post("/api/vms", json=mock_vm_payload)

    r = client.post(
        "/api/vms/bulk",
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
    listing = client.get("/api/vms").json()["items"]
    assert {v["name"] for v in listing} == {"db-01", "app-01", "app-02"}

    # ssh_user round-trips
    app01 = next(v for v in listing if v["name"] == "app-01")
    assert app01["ssh_user"] == "ec2-user"


def test_bulk_create_validates_entries(client):
    r = client.post("/api/vms/bulk", json={"vms": []})
    assert r.status_code == 422


def test_create_vm_accepts_ssh_user(client):
    r = client.post(
        "/api/vms",
        json={
            "name": "db-99",
            "source_hostname": "db-99.local",
            "ssh_user": "rocky",
        },
    )
    assert r.status_code == 201
    assert r.json()["ssh_user"] == "rocky"


def test_create_vm_persists_mtv_mapping_fields(client):
    r = client.post(
        "/api/vms",
        json={
            "name": "db-mtv",
            "source_hostname": "db-mtv.local",
            "vsphere_networks": ["VM Network", "DB Backend"],
            "vsphere_datastores": ["nfs-prod-fast"],
            "target_namespace": "finance-prod",
            "target_storage_class": "ocs-storagecluster-cephfs",
            "target_network_attachment": "db-backend-nad",
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert body["vsphere_networks"] == ["VM Network", "DB Backend"]
    assert body["vsphere_datastores"] == ["nfs-prod-fast"]
    assert body["target_namespace"] == "finance-prod"
    assert body["target_storage_class"] == "ocs-storagecluster-cephfs"
    assert body["target_network_attachment"] == "db-backend-nad"


def test_create_vm_defaults_mtv_lists_to_empty(client, mock_vm_payload):
    r = client.post("/api/vms", json=mock_vm_payload)
    assert r.status_code == 201
    body = r.json()
    assert body["vsphere_networks"] == []
    assert body["vsphere_datastores"] == []
    assert body["target_namespace"] is None


def test_wave_mtv_yaml_endpoint_renders_three_documents(client, db_session):
    """End-to-end: enroll two VMs, hand-build a plan row, hit the YAML route."""
    from app.models.plan import MigrationPlan

    db_session.add_all(
        [
            _vm(
                "db-prod-01",
                vsphere_networks=["DB Backend"],
                vsphere_datastores=["nfs-prod-fast"],
                target_namespace="finance-prod",
                target_storage_class="ocs-storagecluster-cephfs",
                target_network_attachment="db-backend-nad",
            ),
            _vm(
                "db-prod-02",
                vsphere_networks=["DB Backend"],
                vsphere_datastores=["nfs-prod-fast"],
                target_namespace="finance-prod",
                target_storage_class="ocs-storagecluster-cephfs",
                target_network_attachment="db-backend-nad",
            ),
        ]
    )
    db_session.commit()
    from app.models.vm import VM

    ids = [vm.id for vm in db_session.query(VM).order_by(VM.id).all()]

    plan = MigrationPlan(
        vm_ids=ids,
        waves=[
            {
                "wave_number": 1,
                "vm_ids": ids,
                "rationale": "Both DB VMs share DB Backend portgroup and nfs-prod-fast",
                "estimated_risk": "high",
            }
        ],
        summary="DB tier",
        model="test-model",
    )
    db_session.add(plan)
    db_session.commit()

    r = client.get(f"/api/plans/{plan.id}/waves/1/mtv-yaml")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/yaml")
    body = r.text
    assert body.count("apiVersion: forklift.konveyor.io/v1beta1") == 3
    assert "kind: NetworkMap" in body
    assert "kind: StorageMap" in body
    assert "kind: Plan" in body
    assert "warm: true" in body
    assert "DB Backend" in body
    assert "nfs-prod-fast" in body


def _vm(name: str, **kwargs):
    """Helper that mirrors the VM model defaults so tests stay terse."""
    from app.models.vm import VM

    return VM(name=name, source_hostname=f"{name}.local", **kwargs)


# ---------- /api/templates ----------


def test_csv_template_endpoint_returns_csv_attachment(client):
    r = client.get("/api/templates/csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert 'filename="vm-inventory-template.csv"' in r.headers["content-disposition"]
    body = r.text
    # Header row must contain the canonical columns the frontend expects.
    first_line = body.splitlines()[0]
    for col in ("hostname", "vsphere_networks", "vsphere_datastores", "target_namespace"):
        assert col in first_line


def test_csv_template_endpoint_returns_500_when_missing(client, monkeypatch):
    """If the file is missing, the endpoint should fail loudly, not 200 empty."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "csv_template_path", "/nonexistent/template.csv")
    r = client.get("/api/templates/csv")
    assert r.status_code == 500
    assert "not found" in r.json()["detail"].lower()
