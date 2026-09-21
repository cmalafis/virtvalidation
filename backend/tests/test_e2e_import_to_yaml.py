"""Import → map → plan → MTV YAML on the 1,000-VM fixture.

The acceptance path for "the pieces that build waves and YAML work
together": every stage runs through the public API, every emitted document
is validated offline against the vendored forklift CRD schemas, and the
wave invariants are checked across the whole fleet rather than on a toy
input.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from collections import Counter
from pathlib import Path

import pytest
import yaml

from app.core.mtv_validate import validate_documents
from app.core.wave_skeleton import MAX_VMS_PER_WAVE

FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "rvtools-1000.xlsx"
VCENTERS = [f"vc-site{i:02d}.hospital.example" for i in (1, 2, 3)]


@pytest.fixture
def planned(client, tmp_path, monkeypatch):
    monkeypatch.setenv("IMPORT_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr("app.core.config.settings.llm_backend_type", "mock")
    from app.core.llm import runtime
    from app.core.llm.factory import reset_backend_cache

    reset_backend_cache()
    # The active-backend type is cached in-process for 60s. An earlier test
    # that selected a real backend would otherwise leave ~100 waves each
    # waiting on a connection to a server that isn't there.
    runtime.invalidate()
    monkeypatch.setattr(runtime, "get_active_backend_type", lambda db=None: "mock")

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

    target = client.post(
        "/api/sources/targets",
        json={"name": "ocp-east", "api_endpoint": "https://api.ocp-east.example:6443"},
    ).json()["id"]
    for vid in vc.values():
        m = client.post(
            "/api/mappings",
            json={"name": f"m{vid}", "vcenter_source_id": vid, "ocp_target_id": target},
        ).json()
        sig = client.get(f"/api/mappings/{m['id']}/source-signals").json()
        names = lambda rows: [r["name"] if isinstance(r, dict) else r for r in rows]  # noqa: E731
        body = {
            "network_mappings": [
                {
                    "source_network": n,
                    "target_network_name": f"{n}-nad",
                    "target_network_type": "nad",
                }
                for n in names(sig["networks"])
            ],
            "storage_mappings": [
                {
                    "source_datastore": d,
                    "target_storage_class": "ocs-virt",
                    "access_mode": "ReadWriteMany",
                }
                for d in names(sig["datastores"])
            ],
            "namespace_mappings": [{"criteria": "default", "target_namespace": "vms-prod"}],
        }
        assert client.patch(f"/api/mappings/{m['id']}", json=body).json()["status"] == "complete"

    # The two fixture VMs with no datastore / no network can't be mapped;
    # plan everything that can be.
    vms = client.get("/api/vms?limit=1000").json()["items"]
    plannable = [v for v in vms if v["vsphere_networks"] and v["vsphere_datastores"]]
    assert len(plannable) == 998

    started = time.monotonic()
    r = client.post("/api/plans", json={"name": "fleet", "vm_ids": [v["id"] for v in plannable]})
    assert r.status_code == 202, r.text
    elapsed = time.monotonic() - started
    plans = [client.get(f"/api/plans/{p['id']}").json() for p in r.json()["plans"]]
    return {
        "client": client,
        "plans": plans,
        "vms": {v["id"]: v for v in plannable},
        "elapsed": elapsed,
    }


def test_every_vm_lands_in_exactly_one_wave_and_no_wave_mixes_vcenters(planned):
    assert all(p["status"] == "complete" for p in planned["plans"]), [
        p.get("error_message") for p in planned["plans"]
    ]
    assert planned["elapsed"] < 60, f"998-VM plan generation took {planned['elapsed']:.1f}s"

    placed: Counter = Counter()
    for plan in planned["plans"]:
        for wave in plan["waves"]:
            ids = wave["vm_ids"]
            assert 0 < len(ids) <= MAX_VMS_PER_WAVE
            assert (
                len({planned["vms"][i]["source_vcenter_id"] for i in ids}) == 1
            ), f"plan {plan['id']} wave {wave['wave_number']} spans vCenters"
            assert wave.get("method"), "every wave records how its annotation was produced"
            placed.update(ids)
    assert set(placed) == set(planned["vms"]) and set(placed.values()) == {1}


def test_every_emitted_document_passes_the_real_crd_schemas(planned):
    client = planned["client"]
    # VM names repeat across vCenters, so a name maps to a set of MoRefs.
    morefs: dict[str, set[str]] = {}
    for v in planned["vms"].values():
        morefs.setdefault(v["name"], set()).add(v["moref"])
    waves = 0
    for plan in planned["plans"]:
        z = client.get(f"/api/plans/{plan['id']}/yaml")
        assert z.status_code == 200, z.text
        bundle = zipfile.ZipFile(io.BytesIO(z.content))
        files = [n for n in bundle.namelist() if n.endswith(".yaml")]
        assert len(files) == len(plan["waves"])

        readme = bundle.read("README.md").decode()
        assert "--dry-run=server" in readme and "kind: Migration" in readme
        assert "source provider `vc-site0" in readme

        for name in files:
            text = bundle.read(name).decode()
            assert validate_documents(text) == [], name
            netmap, storagemap, mtv_plan = yaml.safe_load_all(text)
            waves += 1
            spec = mtv_plan["spec"]
            assert spec["type"] == "cold" and "warm" not in spec
            assert spec["targetNamespace"] == "vms-prod"
            for vm in spec["vms"]:
                # id is the MoRef from the export, name is the source name verbatim
                assert vm["id"] in morefs[vm["name"]] and vm["id"].startswith("vm-")
            for entry in storagemap["spec"]["map"]:
                assert entry["destination"]["accessMode"] == "ReadWriteMany"
            for entry in netmap["spec"]["map"]:
                assert entry["destination"] == {
                    "type": "multus",
                    "name": f"{entry['source']['name']}-nad",
                    "namespace": "vms-prod",
                }
    assert waves > 90


def test_warm_plan_emits_type_warm_and_warns_in_the_readme(planned):
    client = planned["client"]
    first = planned["plans"][0]
    client.delete(f"/api/plans/{first['id']}")
    # Warm needs CBT — take VMs the assessment says can do it.
    fit = {
        v["id"]
        for v in planned["vms"].values()
        if "vmware.changed_block_tracking.disabled"
        not in {f["id"] for f in v["assessment_findings"]}
    }
    ids = [i for w in first["waves"] for i in w["vm_ids"] if i in fit][:15]
    unfit = [i for w in first["waves"] for i in w["vm_ids"] if i not in fit][:1]
    refused = client.post("/api/plans", json={"vm_ids": unfit, "migration_type": "warm"})
    assert refused.status_code == 422 and "changed_block_tracking" in refused.json()["detail"]
    r = client.post("/api/plans", json={"name": "warm", "vm_ids": ids, "migration_type": "warm"})
    assert r.status_code == 202, r.text
    plan = client.get(f"/api/plans/{r.json()['plans'][0]['id']}").json()
    assert plan["migration_type"] == "warm"
    bundle = zipfile.ZipFile(io.BytesIO(client.get(f"/api/plans/{plan['id']}/yaml").content))
    assert "Changed Block Tracking" in bundle.read("README.md").decode()
    text = bundle.read(next(n for n in bundle.namelist() if n.endswith(".yaml"))).decode()
    assert list(yaml.safe_load_all(text))[2]["spec"]["type"] == "warm"


def test_validator_rejects_the_bugs_it_exists_for():
    good = {
        "apiVersion": "forklift.konveyor.io/v1beta1",
        "kind": "Plan",
        "metadata": {"name": "p", "namespace": "openshift-mtv"},
        "spec": {
            "targetNamespace": "prod",
            "provider": {"source": {"name": "a"}, "destination": {"name": "b"}},
            "map": {"network": {"name": "n"}, "storage": {"name": "s"}},
            "vms": [{"id": "vm-12", "name": "web"}],
        },
    }
    assert validate_documents(yaml.safe_dump(good)) == []

    def problems(**spec_over):
        return " | ".join(
            validate_documents(yaml.safe_dump({**good, "spec": {**good["spec"], **spec_over}}))
        )

    assert "not a vSphere MoRef" in problems(vms=[{"id": "APP-DB-01", "name": "app-db-01"}])
    assert "SOURCE namespace" in problems(vms=[{"id": "vm-1", "namespace": "other"}])
    assert "'lukewarm' is not one of" in problems(type="lukewarm")
    assert "targetNamespace" in " | ".join(
        validate_documents(
            yaml.safe_dump(
                {**good, "spec": {k: v for k, v in good["spec"].items() if k != "targetNamespace"}}
            )
        )
    )
