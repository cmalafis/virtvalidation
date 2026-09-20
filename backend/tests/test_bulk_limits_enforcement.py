"""End-to-end checks that the bulk endpoints actually honor the
centralized limits.

These exercise the schema-level Field constraints (the Pydantic
``max_length=`` against the constants in ``app.core.limits``). A
regression that silently downgrades the cap to 500 would surface
here as a 422 on a 1000-VM payload.
"""

from __future__ import annotations

from app.core.limits import (
    MAX_VMS_PER_BULK_CREATE,
    MAX_VMS_PER_BULK_DELETE,
    MAX_VMS_PER_RVTOOLS_IMPORT,
)


def _vm_row(i: int) -> dict:
    return {
        "name": f"vm-{i:05d}",
        "source_hostname": f"vm-{i:05d}.corp",
        "ip_address": "10.0.0.5",
    }


# ---------------------------------------------------------------------------
# Bulk create
# ---------------------------------------------------------------------------
def test_bulk_create_accepts_1000_vms(client):
    payload = {"vms": [_vm_row(i) for i in range(1000)]}
    r = client.post("/api/vms/bulk", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1000


def test_bulk_create_accepts_up_to_max(client):
    """Sanity check the upper bound — sized to MAX so a regression that
    drops the constant fails here loudly."""
    payload = {"vms": [_vm_row(i) for i in range(MAX_VMS_PER_BULK_CREATE)]}
    r = client.post("/api/vms/bulk", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == MAX_VMS_PER_BULK_CREATE


def test_bulk_create_rejects_over_max_with_readable_422(client):
    payload = {"vms": [_vm_row(i) for i in range(MAX_VMS_PER_BULK_CREATE + 1)]}
    r = client.post("/api/vms/bulk", json=payload)
    assert r.status_code == 422
    # Pydantic emits a structured array — the UI's fetchJSON flattens
    # it into "field.path: msg". Verify the field is correctly named
    # so the operator can act on it.
    body = r.json()
    assert isinstance(body["detail"], list)
    first = body["detail"][0]
    assert "vms" in (first["loc"] or [])
    assert "too long" in first["msg"].lower() or "at most" in first["msg"].lower()


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------
def test_bulk_delete_rejects_over_max(client):
    payload = {"vm_ids": list(range(1, MAX_VMS_PER_BULK_DELETE + 2))}
    r = client.request("DELETE", "/api/vms", json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    assert "vm_ids" in (detail[0]["loc"] or [])


# ---------------------------------------------------------------------------
# RVTools import
# ---------------------------------------------------------------------------
def test_rvtools_import_rejects_over_max(client):
    """The auto-link upload endpoint caps the ``vms`` array at
    MAX_VMS_PER_RVTOOLS_IMPORT. Beyond that, the operator must run
    the upload as multiple chunks (deferred per spec). The 422 here
    must surface a readable field path the UI can render."""
    payload = {
        "vms": [
            {
                "name": f"vm-{i:05d}",
                "source_hostname": f"vm-{i:05d}.corp",
                "source_vcenter_hostname": "vc-east-01.corp.local",
            }
            for i in range(MAX_VMS_PER_RVTOOLS_IMPORT + 1)
        ],
        "vcenter_mapping": {"vc-east-01.corp.local": 1},
    }
    r = client.post("/api/rvtools/upload-multi-vcenter", json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    assert any("vms" in (e.get("loc") or []) for e in detail)


# ---------------------------------------------------------------------------
# Sanity-check error message shape across the stack
# ---------------------------------------------------------------------------
def test_422_payload_is_array_of_objects_with_loc_and_msg(client):
    """The frontend's fetchJSON flattens detail to "field.path: msg"
    — that assumes ``detail`` is a list of objects with ``loc`` +
    ``msg`` keys. Verify the contract end-to-end so a Pydantic-major
    upgrade doesn't silently break the UI's error rendering."""
    payload = {"vms": []}  # empty list violates min_length=1
    r = client.post("/api/vms/bulk", json=payload)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    assert detail
    first = detail[0]
    assert "loc" in first
    assert "msg" in first
    assert isinstance(first["loc"], list)
