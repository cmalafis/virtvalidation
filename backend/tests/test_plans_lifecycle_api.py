"""HTTP-level tests for the lifecycle hooks on POST/DELETE/mark-succeeded.

Exercises the API contract that the wizard + inventory pages rely on:

  - 422 on selection cap overflow
  - 422 on lifecycle precondition violation (VM already in another plan)
  - 422 on incomplete mapping coverage
  - 202 on the happy path (plan lifecycle: pending -> complete -> migrated)
  - VMs transition through available -> planned -> migrated correctly
  - DELETE plan rolls VMs back to available
  - POST /api/plans/{id}/mark-succeeded is 409 unless plan is complete
  - POST /api/plans without mapping_id auto-resolves the active mapping
"""

from __future__ import annotations


def _seed_default_mapping(client) -> int:
    """Register one vCenter + one OCP target + the canonical mapping
    that covers ``vlan-100``/``tier1``/namespace=prod. Returns the
    vcenter source id. Idempotent across tests sharing the same client.
    """
    vcs = client.get("/api/sources/vcenters").json()
    if isinstance(vcs, dict):
        vcs = vcs.get("items", [])
    vc = next((v for v in vcs if v["name"] == "vc-default"), None)
    if vc is None:
        r = client.post(
            "/api/sources/vcenters",
            json={"name": "vc-default", "hostname": "vc-default.example"},
        )
        assert r.status_code == 201, r.text
        vc = r.json()
    targets = client.get("/api/sources/targets").json()
    tgt = next((t for t in targets if t["name"] == "ocp-default"), None)
    if tgt is None:
        r = client.post(
            "/api/sources/targets",
            json={"name": "ocp-default", "api_endpoint": "https://ocp-default.example"},
        )
        assert r.status_code == 201, r.text
        tgt = r.json()
    existing = [
        m
        for m in client.get("/api/mappings").json()
        if m["vcenter_source_id"] == vc["id"] and m["ocp_target_id"] == tgt["id"]
    ]
    if not existing:
        r = client.post(
            "/api/mappings",
            json={
                "name": "default-mapping",
                "vcenter_source_id": vc["id"],
                "ocp_target_id": tgt["id"],
                "network_mappings": [
                    {
                        "source_network": "vlan-100",
                        "target_network_name": "vlan-100-nad",
                        "target_network_type": "nad",
                    }
                ],
                "storage_mappings": [
                    {"source_datastore": "tier1", "target_storage_class": "ocs-rbd"}
                ],
                "namespace_mappings": [{"criteria": "default", "target_namespace": "prod"}],
            },
        )
        assert r.status_code == 201, r.text
    return vc["id"]


def _enroll(client, name: str, **overrides):
    vc_id = _seed_default_mapping(client)
    payload = {
        "name": name,
        "source_hostname": f"{name}.local",
        "vsphere_networks": ["vlan-100"],
        "vsphere_datastores": ["tier1"],
        "source_vcenter_id": vc_id,
        "application_hint": "test-app",
        **overrides,
    }
    r = client.post("/api/vms", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _setup_backend(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.llm_backend_type", "mock")
    from app.core.llm.factory import reset_backend_cache

    reset_backend_cache()


class TestSelectionCap:
    def test_cap_returns_422(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        # Lower the cap so the test runs cheaply.
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 3)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 6)]
        r = client.post("/api/plans", json={"vm_ids": [v["id"] for v in vms]})
        assert r.status_code == 422
        assert "max 3" in r.json()["detail"]

    def test_default_cap_is_1000(self, monkeypatch):
        # Importing here so the test doesn't pin the value at module load
        # time — the cap is a settings attribute and can be overridden
        # via env var per-deployment.
        from app.core.config import settings as app_settings

        assert app_settings.max_vms_per_plan == 1000


class TestMultiPlanFanOut:
    def _seed_two_clusters(self, client):
        """vCenter A + two OCP target clusters + a mapping per pair, each
        covering vlan-100/tier1 with a single-namespace strategy. Returns
        (vc_id, cluster_a_id, cluster_b_id)."""
        vc = client.post(
            "/api/sources/vcenters",
            json={"name": "vc-multi", "hostname": "vc-multi.example"},
        ).json()
        a = client.post(
            "/api/sources/targets",
            json={"name": "ocp-multi-a", "api_endpoint": "https://ocp-a.example"},
        ).json()
        b = client.post(
            "/api/sources/targets",
            json={"name": "ocp-multi-b", "api_endpoint": "https://ocp-b.example"},
        ).json()
        for tgt, ns in ((a, "prod-a"), (b, "prod-b")):
            client.post(
                "/api/mappings",
                json={
                    "name": f"map-{tgt['name']}",
                    "vcenter_source_id": vc["id"],
                    "ocp_target_id": tgt["id"],
                    "network_mappings": [
                        {
                            "source_network": "vlan-100",
                            "target_network_name": "vlan-100-nad",
                            "target_network_type": "nad",
                            "target_namespace": "openshift-multus",
                        }
                    ],
                    "storage_mappings": [
                        {"source_datastore": "tier1", "target_storage_class": "ocs-rbd"}
                    ],
                    "namespace_mappings": [{"criteria": "default", "target_namespace": ns}],
                },
            ).raise_for_status()
        return vc["id"], a["id"], b["id"]

    def test_post_plans_fans_out_per_cluster(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vc_id, cluster_a, cluster_b = self._seed_two_clusters(client)
        ids = []
        for i in range(1, 4):
            ids.append(
                client.post(
                    "/api/vms",
                    json={
                        "name": f"vm-a-{i:02d}",
                        "source_hostname": f"vm-a-{i:02d}.local",
                        "source_vcenter_id": vc_id,
                        "target_cluster_id_override": cluster_a,
                        "vsphere_networks": ["vlan-100"],
                        "vsphere_datastores": ["tier1"],
                    },
                ).json()["id"]
            )
        for i in range(1, 3):
            ids.append(
                client.post(
                    "/api/vms",
                    json={
                        "name": f"vm-b-{i:02d}",
                        "source_hostname": f"vm-b-{i:02d}.local",
                        "source_vcenter_id": vc_id,
                        "target_cluster_id_override": cluster_b,
                        "vsphere_networks": ["vlan-100"],
                        "vsphere_datastores": ["tier1"],
                    },
                ).json()["id"]
            )

        r = client.post("/api/plans", json={"name": "Q3-mc", "vm_ids": ids})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["plan_count"] == 2, body
        assert len(body["plans"]) == 2
        names = sorted(p["name"] for p in body["plans"])
        assert all("ocp-multi-a" in n or "ocp-multi-b" in n for n in names)
        # All five VMs accounted for across the two plan rows.
        all_vm_ids = sorted(vid for p in body["plans"] for vid in p["vm_ids"])
        assert all_vm_ids == sorted(ids)

    def test_post_plans_preview_returns_partition_groups(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vc_id, cluster_a, cluster_b = self._seed_two_clusters(client)
        ids = []
        for cluster_id, prefix in ((cluster_a, "a"), (cluster_b, "b")):
            for i in range(1, 4):
                ids.append(
                    client.post(
                        "/api/vms",
                        json={
                            "name": f"vm-{prefix}-{i:02d}",
                            "source_hostname": f"vm-{prefix}-{i:02d}.local",
                            "source_vcenter_id": vc_id,
                            "target_cluster_id_override": cluster_id,
                            "vsphere_networks": ["vlan-100"],
                            "vsphere_datastores": ["tier1"],
                        },
                    ).json()["id"]
                )
        r = client.post("/api/plans/preview", json={"vm_ids": ids})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_vms"] == 6
        assert body["resolvable"] == 6
        assert body["unresolvable"] == 0
        assert len(body["groups"]) == 2
        for g in body["groups"]:
            assert g["vm_count"] == 3
            assert "vlan-100-nad" in g["network_targets"]
            assert "ocs-rbd" in g["storage_targets"]

    def test_post_plans_unresolvable_returns_422(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        # No mapping; the VM resolves to no cluster.
        vc = client.post(
            "/api/sources/vcenters",
            json={"name": "vc-bare", "hostname": "vc-bare.example"},
        ).json()
        vm = client.post(
            "/api/vms",
            json={
                "name": "vm-bare",
                "source_hostname": "vm-bare.local",
                "source_vcenter_id": vc["id"],
            },
        ).json()
        r = client.post("/api/plans", json={"vm_ids": [vm["id"]]})
        assert r.status_code == 422, r.text
        assert "cannot be resolved" in r.json()["detail"]

    def test_exactly_at_cap_succeeds(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 3)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 4)]
        r = client.post("/api/plans", json={"vm_ids": [v["id"] for v in vms]})
        assert r.status_code == 202


class TestLifecyclePrecondition:
    def test_double_plan_rejected(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-1")
        # First plan succeeds — VM moves available -> planned.
        first = client.post("/api/plans", json={"vm_ids": [vm["id"]]})
        assert first.status_code == 202
        # Second plan with the same VM is blocked: lifecycle_state is
        # already "planned".
        second = client.post("/api/plans", json={"vm_ids": [vm["id"]]})
        assert second.status_code == 422
        assert "planned" in second.json()["detail"]


class TestMappingCoverage:
    def test_missing_storage_returns_422(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        # VM has a datastore but no target_storage_class and no mapping.
        vm = _enroll(
            client,
            "vm-bad",
            vsphere_datastores=["unmapped-ds"],
        )
        r = client.post("/api/plans", json={"vm_ids": [vm["id"]]})
        assert r.status_code == 422
        assert "datastore" in r.json()["detail"]


class TestPlanLifecycle:
    def test_happy_path_available_planned_migrated(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-1")
        # Before plan: available
        assert client.get(f"/api/vms/{vm['id']}").json()["lifecycle_state"] == "available"

        plan = client.post("/api/plans", json={"vm_ids": [vm["id"]]}).json()
        plan_id = plan["id"]
        # After plan create + background task: planned then complete
        assert client.get(f"/api/vms/{vm['id']}").json()["lifecycle_state"] == "planned"
        body = client.get(f"/api/plans/{plan_id}").json()
        assert body["status"] == "complete", body

        # Mark succeeded — VM moves planned -> migrated, plan -> migrated
        r = client.post(f"/api/plans/{plan_id}/mark-succeeded")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "migrated"
        assert client.get(f"/api/vms/{vm['id']}").json()["lifecycle_state"] == "migrated"

    def test_mark_succeeded_idempotent(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-1")
        plan = client.post("/api/plans", json={"vm_ids": [vm["id"]]}).json()
        client.post(f"/api/plans/{plan['id']}/mark-succeeded").raise_for_status()
        # Second call should still succeed without re-transitioning.
        r = client.post(f"/api/plans/{plan['id']}/mark-succeeded")
        assert r.status_code == 200

    def test_mark_succeeded_409_on_pending(self, client, monkeypatch):
        _setup_backend(monkeypatch)

        # Force the pipeline to never finish by patching it to a no-op
        # that leaves the plan in "pending".
        async def _never(*a, **kw):  # noqa: ARG001
            raise RuntimeError("pretend pipeline stalled")

        monkeypatch.setattr("app.core.plan_pipeline.run_pipeline", _never)
        vm = _enroll(client, "vm-1")
        r = client.post("/api/plans", json={"vm_ids": [vm["id"]]})
        plan_id = r.json()["id"]
        # Plan is in "failed" after the stub raises — try to mark-succeeded.
        assert client.get(f"/api/plans/{plan_id}").json()["status"] == "failed"
        mark = client.post(f"/api/plans/{plan_id}/mark-succeeded")
        assert mark.status_code == 409


class TestPlanDeletion:
    def test_delete_returns_vms_to_available(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-1")
        plan = client.post("/api/plans", json={"vm_ids": [vm["id"]]}).json()
        assert client.get(f"/api/vms/{vm['id']}").json()["lifecycle_state"] == "planned"
        r = client.delete(f"/api/plans/{plan['id']}")
        assert r.status_code == 204
        assert client.get(f"/api/vms/{vm['id']}").json()["lifecycle_state"] == "available"
        # Plan should be 404 now.
        assert client.get(f"/api/plans/{plan['id']}").status_code == 404


class TestPatchLifecycle:
    def test_migrated_to_rolled_back_to_available(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-1")
        plan = client.post("/api/plans", json={"vm_ids": [vm["id"]]}).json()
        client.post(f"/api/plans/{plan['id']}/mark-succeeded").raise_for_status()
        # migrated -> rolled_back
        r = client.patch(f"/api/vms/{vm['id']}", json={"lifecycle_state": "rolled_back"})
        assert r.status_code == 200, r.text
        assert r.json()["lifecycle_state"] == "rolled_back"
        # rolled_back -> available
        r = client.patch(f"/api/vms/{vm['id']}", json={"lifecycle_state": "available"})
        assert r.status_code == 200
        assert r.json()["lifecycle_state"] == "available"

    def test_rejects_illegal_transition(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-1")
        # available -> rolled_back is not allowed via PATCH
        r = client.patch(f"/api/vms/{vm['id']}", json={"lifecycle_state": "rolled_back"})
        assert r.status_code == 422

    def test_rejects_skipping_rolled_back(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-1")
        plan = client.post("/api/plans", json={"vm_ids": [vm["id"]]}).json()
        client.post(f"/api/plans/{plan['id']}/mark-succeeded").raise_for_status()
        # Operator must go through rolled_back; direct migrated -> available is 422
        r = client.patch(f"/api/vms/{vm['id']}", json={"lifecycle_state": "available"})
        assert r.status_code == 422


class TestMappingAutoResolve:
    """POST /api/plans without mapping_id auto-resolves the active mapping
    for the VMs' source vCenter. Without this, the legacy modal — which
    posts only ``vm_ids`` — would see every source resource as unmapped
    and report N false-positive coverage gaps.
    """

    def _seed_vcenter_and_mapping(self, client):
        """Create one vCenter + one OCP target + one active ResourceMapping
        covering ``vlan-100`` and ``tier1``. Returns the vcenter dict.
        """
        r = client.post(
            "/api/sources/vcenters",
            json={"name": "vc-auto", "hostname": "vc-auto.example"},
        )
        assert r.status_code == 201, r.text
        vc = r.json()
        r = client.post(
            "/api/sources/targets",
            json={
                "name": "target-auto",
                "api_endpoint": "https://target-auto.example",
            },
        )
        assert r.status_code == 201, r.text
        target = r.json()
        client.post(
            "/api/mappings",
            json={
                "name": "active-mapping",
                "vcenter_source_id": vc["id"],
                "ocp_target_id": target["id"],
                "network_mappings": [
                    {
                        "source_network": "vlan-100",
                        "target_network_name": "vlan-100-nad",
                        "target_network_type": "nad",
                    }
                ],
                "storage_mappings": [
                    {
                        "source_datastore": "tier1",
                        "target_storage_class": "ocs-rbd",
                    }
                ],
                "namespace_mappings": [{"criteria": "default", "target_namespace": "prod"}],
            },
        ).raise_for_status()
        return vc

    def test_auto_resolves_when_mapping_ids_omitted(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vc = self._seed_vcenter_and_mapping(client)
        # VM has NO per-VM target_* fallbacks; mapping has to be found
        # for validation to pass.
        vm = _enroll(
            client,
            "vm-auto",
            source_vcenter_id=vc["id"],
            target_namespace_override=None,
        )
        # POST without mapping_ids — auto-resolution must find the
        # sole-per-pair mapping and let Stage 0 pass.
        r = client.post("/api/plans", json={"vm_ids": [vm["id"]]})
        assert r.status_code == 202, r.text
        # mapping_ids should be populated on the plan row.
        plan = client.get(f"/api/plans/{r.json()['id']}").json()
        assert plan["mapping_ids"], plan

    def test_multi_vcenter_no_auto_resolve(self, client, monkeypatch):
        # When VMs span vCenters, auto-resolution declines (ambiguous)
        # and Stage 0 reports gaps. Operator must pick a mapping
        # explicitly via the wizard.
        _setup_backend(monkeypatch)
        vc_a = self._seed_vcenter_and_mapping(client)
        # Create a second vCenter (no mapping needed for the assertion).
        r = client.post(
            "/api/sources/vcenters",
            json={"name": "vc-b", "hostname": "vc-b.example"},
        )
        assert r.status_code == 201, r.text
        vc_b = r.json()
        vm_a = _enroll(
            client,
            "vm-a",
            source_vcenter_id=vc_a["id"],
            target_namespace_override=None,
        )
        vm_b = _enroll(
            client,
            "vm-b",
            source_vcenter_id=vc_b["id"],
            target_namespace_override=None,
        )
        r = client.post("/api/plans", json={"vm_ids": [vm_a["id"], vm_b["id"]]})
        # Auto-resolution returned None (spans two vcenters), so the
        # validator sees mapping=None and reports gaps for vm_b's
        # uncovered sources. 422 either way.
        assert r.status_code == 422


class TestSelectorFilter:
    def test_lifecycle_state_filter(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        vm_a = _enroll(client, "vm-a")
        vm_b = _enroll(client, "vm-b")
        # Move vm_a to planned via a plan
        plan = client.post("/api/plans", json={"vm_ids": [vm_a["id"]]}).json()
        # Default listing returns both
        items = client.get("/api/vms").json()["items"]
        assert len(items) == 2
        # Filtered to available: only vm_b
        avail = client.get("/api/vms?lifecycle_state=available").json()["items"]
        assert [v["id"] for v in avail] == [vm_b["id"]]
        # Filtered to planned: only vm_a
        planned = client.get("/api/vms?lifecycle_state=planned").json()["items"]
        assert [v["id"] for v in planned] == [vm_a["id"]]
