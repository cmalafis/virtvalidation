"""HTTP-level tests for the lifecycle hooks on POST/DELETE/mark-succeeded.

Exercises the API contract that the wizard + inventory pages rely on:

  - selections above max_vms_per_plan become a numbered series of plans
  - 422 above the per-request ceiling (MAX_VMS_PER_PLAN_SCOPE)
  - skip_ineligible drops blocked / unresolvable VMs into ``excluded``
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


def _mark_blocked(db_session, vm_id: int) -> None:
    """Give a VM an assessment that blocks cold migration (NVMe controller)."""
    from app.core.assessment import VMFacts, assess
    from app.models.vm import VM

    facts = VMFacts(
        name=f"vm-{vm_id}",
        hardware={
            "v": 1,
            "cbt": True,
            "disks": [
                {
                    "label": "Hard disk 1",
                    "mode": "persistent",
                    "sharing": "sharingNone",
                    "shared_bus": "noSharing",
                    "raw": False,
                    "controller": "NVME controller 0",
                    "datastore": "tier1",
                }
            ],
        },
    )
    doc = assess(facts)
    vm = db_session.get(VM, vm_id)
    vm.assessment = doc.to_dict()
    vm.assessment_status = doc.status
    vm.assessment_finding_ids = [f.id for f in doc.findings]
    db_session.commit()


class TestBatchedSeries:
    """No selection cap: a selection above ``max_vms_per_plan`` becomes
    a series of plans named ``base-1``, ``base-2`` … (one POST, one
    ``series_id``), each holding at most ``max_vms_per_plan`` VMs."""

    def test_over_batch_size_creates_numbered_series(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 3)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 8)]
        ids = [v["id"] for v in vms]
        r = client.post("/api/plans", json={"name": "Q4 Batch", "vm_ids": ids})
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["plan_count"] == 3
        assert body["series_id"]
        assert body["excluded"] == []
        plans = body["plans"]
        assert [p["name"] for p in plans] == ["q4-batch-1", "q4-batch-2", "q4-batch-3"]
        # Balanced, never 3/3/1.
        assert [len(p["vm_ids"]) for p in plans] == [3, 2, 2]
        assert [p["series_index"] for p in plans] == [1, 2, 3]
        assert {p["series_total"] for p in plans} == {3}
        assert len({p["series_id"] for p in plans}) == 1
        assert sorted(vid for p in plans for vid in p["vm_ids"]) == sorted(ids)
        # Every selected VM is committed to a plan.
        for vid in ids:
            assert client.get(f"/api/vms/{vid}").json()["lifecycle_state"] != "available"
        # The series survives a round-trip through GET.
        got = client.get(f"/api/plans/{plans[1]['id']}").json()
        assert got["series_index"] == 2 and got["series_total"] == 3
        assert got["excluded_vms"] == []
        # And the list keeps series order among rows created together.
        listed = [p["name"] for p in client.get("/api/plans").json()]
        assert listed.index("q4-batch-1") < listed.index("q4-batch-2") < listed.index("q4-batch-3")

    def test_single_batch_keeps_name_verbatim(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 3)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 4)]
        r = client.post(
            "/api/plans", json={"name": "Exactly Three", "vm_ids": [v["id"] for v in vms]}
        )
        assert r.status_code == 202
        body = r.json()
        assert body["plan_count"] == 1
        assert body["name"] == "Exactly Three"
        assert body["series_index"] == 1 and body["series_total"] == 1

    def test_request_ceiling_returns_422(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        monkeypatch.setattr("app.core.limits.MAX_VMS_PER_PLAN_SCOPE", 2)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 4)]
        r = client.post("/api/plans", json={"vm_ids": [v["id"] for v in vms]})
        assert r.status_code == 422
        assert "per-request ceiling" in r.json()["detail"]
        assert "max 2" in r.json()["detail"]

    def test_default_batch_size_is_1000(self):
        from app.core.config import settings as app_settings

        assert app_settings.max_vms_per_plan == 1000

    def test_settings_surface_both_limits(self, client):
        body = client.get("/api/settings").json()
        assert body["max_vms_per_plan"] == 1000
        assert body["max_vms_per_plan_request"] == 10_000


class TestSkipIneligible:
    def test_blocked_vm_still_422_by_default(self, client, db_session, monkeypatch):
        _setup_backend(monkeypatch)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 4)]
        _mark_blocked(db_session, vms[1]["id"])
        r = client.post("/api/plans", json={"vm_ids": [v["id"] for v in vms]})
        assert r.status_code == 422
        assert "cannot migrate as a cold plan" in r.json()["detail"]

    def test_skip_ineligible_excludes_blocked_vm(self, client, db_session, monkeypatch):
        _setup_backend(monkeypatch)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 4)]
        blocked_id = vms[1]["id"]
        _mark_blocked(db_session, blocked_id)
        r = client.post(
            "/api/plans",
            json={"name": "skip", "vm_ids": [v["id"] for v in vms], "skip_ineligible": True},
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["plan_count"] == 1
        assert sorted(body["vm_ids"]) == sorted(v["id"] for v in vms if v["id"] != blocked_id)
        assert len(body["excluded"]) == 1
        assert body["excluded"][0]["vm_id"] == blocked_id
        assert body["excluded"][0]["vm_name"] == "vm-02"
        assert "vv.disk.nvme.detected" in body["excluded"][0]["reason"]
        # Persisted on the row so the detail page can show it later.
        got = client.get(f"/api/plans/{body['id']}").json()
        assert got["excluded_vms"] == body["excluded"]
        # The excluded VM was never committed to the plan.
        assert client.get(f"/api/vms/{blocked_id}").json()["lifecycle_state"] == "available"
        # And it is auditable.
        audits = client.get("/api/audit?limit=50").json()
        rows = audits.get("items", audits) if isinstance(audits, dict) else audits
        assert any(a.get("action") == "plan.vms_excluded" for a in rows)

    def test_skip_ineligible_everything_excluded_is_422(self, client, db_session, monkeypatch):
        _setup_backend(monkeypatch)
        vm = _enroll(client, "vm-01")
        _mark_blocked(db_session, vm["id"])
        r = client.post("/api/plans", json={"vm_ids": [vm["id"]], "skip_ineligible": True})
        assert r.status_code == 422
        assert "Every selected VM was excluded" in r.json()["detail"]

    def test_skip_ineligible_excludes_unresolvable_vm(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        good = _enroll(client, "vm-good")
        vc = client.post(
            "/api/sources/vcenters",
            json={"name": "vc-bare-skip", "hostname": "vc-bare-skip.example"},
        ).json()
        bare = client.post(
            "/api/vms",
            json={
                "name": "vm-bare",
                "source_hostname": "vm-bare.local",
                "source_vcenter_id": vc["id"],
            },
        ).json()
        r = client.post(
            "/api/plans",
            json={"vm_ids": [good["id"], bare["id"]], "skip_ineligible": True},
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["vm_ids"] == [good["id"]]
        assert [e["vm_id"] for e in body["excluded"]] == [bare["id"]]
        assert "no target cluster/namespace" in body["excluded"][0]["reason"]

    def test_skip_ineligible_excludes_mapping_gap_vm(self, client, monkeypatch):
        """A VM on a network the mapping doesn't cover is a Stage 0 gap.
        Without skip_ineligible that 422s the whole call; with it the VM
        is excluded by name with the unmapped source and the rest plan."""
        _setup_backend(monkeypatch)
        good = _enroll(client, "vm-good")
        odd = _enroll(client, "vm-odd-net", vsphere_networks=["vlan-999"])
        r = client.post("/api/plans", json={"vm_ids": [good["id"], odd["id"]]})
        assert r.status_code == 422 and "vlan-999" in r.json()["detail"]
        r = client.post(
            "/api/plans", json={"vm_ids": [good["id"], odd["id"]], "skip_ineligible": True}
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["vm_ids"] == [good["id"]]
        assert [e["vm_id"] for e in body["excluded"]] == [odd["id"]]
        assert "network 'vlan-999'" in body["excluded"][0]["reason"]
        assert client.get(f"/api/vms/{odd['id']}").json()["lifecycle_state"] == "available"
        # Preview reports the same exclusion before generation.
        pv = client.post("/api/plans/preview", json={"vm_ids": [good["id"], odd["id"]]}).json()
        assert [e["vm_id"] for e in pv["excluded"]] == [odd["id"]]
        assert pv["plan_count"] == 1 and pv["plans"][0]["vm_count"] == 1

    def test_preview_lists_batched_plans_and_excluded(self, client, db_session, monkeypatch):
        _setup_backend(monkeypatch)
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 3)
        vms = [_enroll(client, f"vm-{i:02d}") for i in range(1, 6)]
        _mark_blocked(db_session, vms[0]["id"])
        r = client.post(
            "/api/plans/preview", json={"name": "Prev", "vm_ids": [v["id"] for v in vms]}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["batch_size"] == 3
        assert body["plan_count"] == 2
        assert [(p["name"], p["vm_count"], p["series_index"]) for p in body["plans"]] == [
            ("prev-1", 2, 1),
            ("prev-2", 2, 2),
        ]
        assert [e["vm_id"] for e in body["excluded"]] == [vms[0]["id"]]
        # Legacy partition view is unchanged.
        assert body["total_vms"] == 5 and len(body["groups"]) == 1


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

    def test_multi_partition_and_multi_batch_naming(self, client, monkeypatch):
        _setup_backend(monkeypatch)
        monkeypatch.setattr("app.core.config.settings.max_vms_per_plan", 2)
        vc_id, cluster_a, cluster_b = self._seed_two_clusters(client)
        ids = []
        for cluster_id, prefix, n in ((cluster_a, "a", 3), (cluster_b, "b", 2)):
            for i in range(1, n + 1):
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
        r = client.post("/api/plans", json={"name": "Q3 mc", "vm_ids": ids})
        assert r.status_code == 202, r.text
        plans = r.json()["plans"]
        # Cluster A (3 VMs) → 2 batches, cluster B (2 VMs) → 1; numbering is
        # series-wide and the partition slug stays in the middle.
        assert [p["name"] for p in plans] == [
            "q3-mc-vc-multi-ocp-multi-a-prod-a-1",
            "q3-mc-vc-multi-ocp-multi-a-prod-a-2",
            "q3-mc-vc-multi-ocp-multi-b-prod-b-3",
        ]
        assert [len(p["vm_ids"]) for p in plans] == [2, 1, 2]
        assert {p["series_total"] for p in plans} == {3}

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

    def test_empty_mapping_ids_reaches_a_terminal_status(self, client, monkeypatch):
        """Regression: ``mapping_ids: []`` (the operator opting out of
        mappings) used to crash the background task on a removed
        ``plan_row.mapping_id`` attribute, leaving the plan stuck in
        ``validating`` with its VM held as ``planned``."""
        _setup_backend(monkeypatch)
        vm = _enroll(
            client,
            "nomap-01",
            vsphere_networks=[],
            vsphere_datastores=[],
            target_namespace_override="prod",
        )
        r = client.post(
            "/api/plans", json={"name": "No mappings", "vm_ids": [vm["id"]], "mapping_ids": []}
        )
        assert r.status_code == 202, r.text
        body = client.get(f"/api/plans/{r.json()['id']}").json()
        assert body["status"] in ("complete", "failed"), body
        expected = "planned" if body["status"] == "complete" else "available"
        assert client.get(f"/api/vms/{vm['id']}").json()["lifecycle_state"] == expected

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
        # Move vm_a to planned via a plan (response unused; the
        # side-effect on the VM's lifecycle_state is what we assert).
        client.post("/api/plans", json={"vm_ids": [vm_a["id"]]}).raise_for_status()
        # Default listing returns both
        items = client.get("/api/vms").json()["items"]
        assert len(items) == 2
        # Filtered to available: only vm_b
        avail = client.get("/api/vms?lifecycle_state=available").json()["items"]
        assert [v["id"] for v in avail] == [vm_b["id"]]
        # Filtered to planned: only vm_a
        planned = client.get("/api/vms?lifecycle_state=planned").json()["items"]
        assert [v["id"] for v in planned] == [vm_a["id"]]
