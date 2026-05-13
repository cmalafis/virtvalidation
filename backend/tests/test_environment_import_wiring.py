"""Tests for environment auto-detection during VM bulk import.

Pins the wiring that turns the standalone ``detect_environment``
helper from O into a real import-time auto-classifier. The API
must:

  - Run detection when the caller omits ``environment``.
  - Honor explicit ``environment`` values and tag them user_set.
  - Populate ``environment_source`` on every VM created via bulk.
  - Read the new ``vsphere_folder`` / ``vsphere_cluster`` /
    ``custom_attributes`` fields when present.

VM model + bulk schema were extended for this work; tests below
exercise the full pipeline through ``POST /api/vms/bulk``.
"""

from __future__ import annotations


def test_bulk_import_auto_detects_from_folder(client):
    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "ehr-web-01",
                    "source_hostname": "ehr-web-01.local",
                    "vsphere_folder": "/prod/ehr-pro",
                }
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    created = body["created"][0]
    assert created["environment"] == "production"


def test_bulk_import_auto_detects_from_cluster(client):
    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "alpha-server",
                    "source_hostname": "alpha.local",
                    "vsphere_cluster": "dev-cluster-east-01",
                }
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()["created"][0]
    assert body["environment"] == "development"


def test_bulk_import_auto_detects_from_custom_attributes(client):
    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "alpha-server",
                    "source_hostname": "alpha.local",
                    "custom_attributes": {"Environment": "DR"},
                }
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()["created"][0]
    assert body["environment"] == "dr"


def test_bulk_import_explicit_environment_marks_user_set(client, db_session):
    from app.models.vm import VM

    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "alpha",
                    "source_hostname": "alpha.local",
                    "environment": "production",
                }
            ],
        },
    )
    assert r.status_code == 200
    vm_id = r.json()["created"][0]["id"]
    vm = db_session.get(VM, vm_id)
    assert vm.environment == "production"
    assert vm.environment_source == "user_set"


def test_bulk_import_auto_detected_marks_source(client, db_session):
    from app.models.vm import VM

    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "prod-web-01",
                    "source_hostname": "prod-web-01.local",
                }
            ],
        },
    )
    assert r.status_code == 200
    vm_id = r.json()["created"][0]["id"]
    vm = db_session.get(VM, vm_id)
    assert vm.environment == "production"
    assert vm.environment_source == "auto_detected"


def test_bulk_import_unknown_remains_unset(client, db_session):
    from app.models.vm import VM

    # Name doesn't match any prefix / DB heuristic; no folder /
    # cluster / custom_attributes signal. Detector returns UNKNOWN
    # and the importer leaves environment NULL + source="unset".
    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "alpha-thing",
                    "source_hostname": "alpha-thing.local",
                }
            ],
        },
    )
    assert r.status_code == 200
    vm_id = r.json()["created"][0]["id"]
    vm = db_session.get(VM, vm_id)
    assert vm.environment in (None, "")
    assert vm.environment_source == "unset"


def test_bulk_import_persists_new_columns(client, db_session):
    from app.models.vm import VM

    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "ehr-app-01",
                    "source_hostname": "ehr-app-01.local",
                    "vsphere_cluster": "prod-cluster-east",
                    "vsphere_folder": "/prod/ehr-pro",
                    "custom_attributes": {"App": "EHRPro", "Tier": "App"},
                }
            ],
        },
    )
    vm_id = r.json()["created"][0]["id"]
    vm = db_session.get(VM, vm_id)
    assert vm.vsphere_cluster == "prod-cluster-east"
    assert vm.vsphere_folder == "/prod/ehr-pro"
    assert vm.custom_attributes == {"App": "EHRPro", "Tier": "App"}


def test_bulk_import_cluster_loses_to_folder_in_cascade(client):
    """Cascade tier 3 (folder) wins over tier 4 (cluster)."""
    r = client.post(
        "/api/vms/bulk",
        json={
            "vms": [
                {
                    "name": "alpha",
                    "source_hostname": "alpha.local",
                    "vsphere_cluster": "prod-cluster",
                    "vsphere_folder": "/dev/sandbox",
                }
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()["created"][0]
    assert body["environment"] == "development"
