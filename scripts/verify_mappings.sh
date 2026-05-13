#!/usr/bin/env bash
# End-to-end smoke test for the Resource Mapping flow.
#
# Boots the FastAPI app in-process (no podman-compose required), seeds
# a vCenter + target + VMs + mapping via the public API, walks every
# endpoint the editor mounts on, exercises inline target-entity
# creation, the PATCH-validation 422 path, and the delete 409/204
# flow. Each step prints PASS / FAIL; script exits non-zero on any
# FAIL so CI can pin the regression.
#
# Reads LLM_BACKEND_TYPE=mock so the suggestion endpoints are
# exercised without a real Ollama. Falls back to sqlite in /tmp so
# the script never touches the dev or production database.
#
# Usage: scripts/verify_mappings.sh

set -u
set -o pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"

if [[ ! -x .venv/bin/python ]]; then
  echo "FAIL: backend/.venv not found. Run 'python3.12 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt' first." >&2
  exit 2
fi

LLM_BACKEND_TYPE=mock \
DATABASE_URL="sqlite:////tmp/verify_mappings_$$.db" \
OLLAMA_HOST="http://localhost:0" \
OLLAMA_MODEL="mock" \
.venv/bin/python - <<'PYEOF'
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("CSV_TEMPLATE_PATH", str(Path(__file__).resolve().parents[1] / "docs" / "vm-inventory-template.csv"))

failures: list[str] = []


def step(label, fn):
    try:
        fn()
        print(f"PASS  {label}")
    except AssertionError as e:
        failures.append(label)
        print(f"FAIL  {label}\n      {e}")
    except Exception as e:
        failures.append(label)
        print(f"FAIL  {label}\n      {type(e).__name__}: {e}")
        traceback.print_exc()


from fastapi.testclient import TestClient

from app.core import db as _db
from app.core.db import Base
from app.main import app

# Fresh schema. Bypasses Alembic by going straight to create_all on
# our in-memory sqlite — the script is a smoke harness, not a
# migration test. The migration drift test pins that separately.
Base.metadata.create_all(bind=_db.engine)
client = TestClient(app)

state = {}


def create_vcenter():
    r = client.post("/api/sources/vcenters", json={"name": "vc-verify", "hostname": "vc-verify.local"})
    assert r.status_code == 201, f"vcenter create: {r.status_code} {r.text}"
    state["vc_id"] = r.json()["id"]


def create_target():
    r = client.post("/api/sources/targets", json={
        "name": "ocp-verify",
        "api_endpoint": "https://api.ocp-verify.example.com",
        "auth_type": "token",
        "verify_ssl": False,
    })
    assert r.status_code == 201, f"target create: {r.status_code} {r.text}"
    state["target_id"] = r.json()["id"]


def create_vms():
    for i, (name, nets, dss) in enumerate([
        ("vm-a", ["src-prod"], ["src-tier1"]),
        ("vm-b", ["src-prod", "src-dmz"], ["src-tier1"]),
        ("vm-c", ["src-dmz"], ["src-bulk"]),
    ]):
        r = client.post("/api/vms", json={
            "name": name,
            "source_hostname": f"{name}.local",
            "source_vcenter_id": state["vc_id"],
            "vsphere_networks": nets,
            "vsphere_datastores": dss,
        })
        assert r.status_code == 201, f"vm {name}: {r.status_code} {r.text}"


def create_mapping():
    r = client.post("/api/mappings", json={
        "name": "verify-mapping",
        "vcenter_source_id": state["vc_id"],
        "ocp_target_id": state["target_id"],
        "network_mappings": [],
        "storage_mappings": [],
        "namespace_mappings": [],
    })
    assert r.status_code == 201, f"mapping create: {r.status_code} {r.text}"
    state["mapping_id"] = r.json()["id"]


def editor_load_sequence():
    """Replays the five GETs the editor fires on mount. Every one
    must return 200 — the original 500 lived in this sequence."""
    mid = state["mapping_id"]
    tid = state["target_id"]
    vid = state["vc_id"]
    for url in [
        f"/api/mappings/{mid}",
        f"/api/sources/targets/{tid}",
        f"/api/ocp-targets/{tid}/networks",
        f"/api/ocp-targets/{tid}/storage-classes",
        f"/api/vms?source_vcenter_id={vid}&limit=1000",
    ]:
        r = client.get(url)
        assert r.status_code == 200, f"{url} -> {r.status_code} {r.text[:200]}"


def empty_catalog_dropdowns():
    """The editor shows the inline-create CTA when no entities exist."""
    tid = state["target_id"]
    nets = client.get(f"/api/ocp-targets/{tid}/networks").json()
    scs = client.get(f"/api/ocp-targets/{tid}/storage-classes").json()
    assert nets == [], f"expected no networks, got {nets}"
    assert scs == [], f"expected no storage classes, got {scs}"


def inline_create_target_network():
    tid = state["target_id"]
    r = client.post(f"/api/ocp-targets/{tid}/networks", json={
        "name": "prod-vlan-100",
        "network_type": "cudn",  # exercise the enum fix
        "namespace": "openshift-multus",
        "is_default": True,
    })
    assert r.status_code == 201, f"net create: {r.status_code} {r.text}"
    r = client.get(f"/api/ocp-targets/{tid}/networks")
    assert r.status_code == 200, r.text
    names = [n["name"] for n in r.json()]
    assert "prod-vlan-100" in names, f"net not in catalog: {names}"


def inline_create_target_storage_class():
    tid = state["target_id"]
    r = client.post(f"/api/ocp-targets/{tid}/storage-classes", json={
        "name": "ocs-rbd",
        "access_mode": "ReadWriteOnce",
        "is_default": True,
    })
    assert r.status_code == 201, f"sc create: {r.status_code} {r.text}"
    r = client.get(f"/api/ocp-targets/{tid}/storage-classes")
    assert r.status_code == 200, r.text
    names = [s["name"] for s in r.json()]
    assert "ocs-rbd" in names, f"sc not in catalog: {names}"


def patch_rejects_bogus_target_network():
    mid = state["mapping_id"]
    r = client.patch(f"/api/mappings/{mid}", json={
        "network_mappings": [
            {"source_network": "src-prod", "target_network_name": "ghost-net", "target_network_type": "nad"}
        ],
    })
    assert r.status_code == 422, f"expected 422, got {r.status_code} {r.text}"
    assert "ghost-net" in r.json()["detail"], r.json()


def patch_saves_valid_mapping_and_persists():
    mid = state["mapping_id"]
    r = client.patch(f"/api/mappings/{mid}", json={
        "network_mappings": [
            {
                "source_network": "src-prod",
                "target_network_name": "prod-vlan-100",
                "target_network_type": "cudn",
            }
        ],
        "storage_mappings": [
            {"source_datastore": "src-tier1", "target_storage_class": "ocs-rbd"}
        ],
        "namespace_mappings": {"strategy": "single", "single_namespace": "verify-ns"},
        "is_active": True,
    })
    assert r.status_code == 200, f"patch: {r.status_code} {r.text}"
    # Reload and confirm persistence.
    r = client.get(f"/api/mappings/{mid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["network_mappings"][0]["target_network_name"] == "prod-vlan-100"
    assert body["storage_mappings"][0]["target_storage_class"] == "ocs-rbd"
    assert body["is_active"] is True


def delete_409_when_referenced_by_plan():
    """Create a plan that points at the mapping, then DELETE the
    mapping. The 409 body must list the referencing plan."""
    from app.core import db as _db
    from app.models.plan import MigrationPlan

    session = _db.SessionLocal()
    try:
        plan = MigrationPlan(
            name="verify-plan",
            vm_ids=[],
            waves=[],
            model="mock",
            mapping_id=state["mapping_id"],
            status="complete",
        )
        session.add(plan)
        session.commit()
        state["plan_id"] = plan.id
    finally:
        session.close()

    r = client.delete(f"/api/mappings/{state['mapping_id']}")
    assert r.status_code == 409, f"expected 409, got {r.status_code} {r.text}"
    body = r.json()
    detail = body["detail"]
    assert isinstance(detail, dict), f"detail not a dict: {detail}"
    assert detail.get("referenced_by"), f"missing referenced_by: {detail}"
    assert any(row["plan_id"] == state["plan_id"] for row in detail["referenced_by"])


def delete_204_after_unblocking():
    """Drop the plan, retry the delete, then GET 404."""
    from app.core import db as _db
    from app.models.plan import MigrationPlan

    session = _db.SessionLocal()
    try:
        plan = session.get(MigrationPlan, state["plan_id"])
        session.delete(plan)
        session.commit()
    finally:
        session.close()

    r = client.delete(f"/api/mappings/{state['mapping_id']}")
    assert r.status_code == 204, f"expected 204, got {r.status_code} {r.text}"
    r = client.get(f"/api/mappings/{state['mapping_id']}")
    assert r.status_code == 404, r.text


def delete_404_on_missing():
    r = client.delete("/api/mappings/9999")
    assert r.status_code == 404, r.text


steps = [
    ("seed vCenter", create_vcenter),
    ("seed OCP target", create_target),
    ("seed VMs", create_vms),
    ("seed mapping", create_mapping),
    ("editor mount sequence (5 GETs, no 500)", editor_load_sequence),
    ("empty catalogs at start", empty_catalog_dropdowns),
    ("inline-create target network (CUDN, exercises enum fix)", inline_create_target_network),
    ("inline-create target storage class", inline_create_target_storage_class),
    ("PATCH rejects bogus target_network_name with 422", patch_rejects_bogus_target_network),
    ("PATCH persists valid mapping; GET reads it back", patch_saves_valid_mapping_and_persists),
    ("DELETE returns 409 with referenced_by when a plan points at the mapping", delete_409_when_referenced_by_plan),
    ("DELETE returns 204 after removing the referencing plan", delete_204_after_unblocking),
    ("DELETE on a missing id returns 404", delete_404_on_missing),
]

for label, fn in steps:
    step(label, fn)

print()
print(f"{len(steps) - len(failures)} / {len(steps)} steps passed")
sys.exit(1 if failures else 0)
PYEOF

rc=$?
rm -f /tmp/verify_mappings_$$.db
exit $rc
