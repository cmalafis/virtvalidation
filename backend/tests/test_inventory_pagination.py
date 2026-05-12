"""Tests for the paginated inventory listing + facets + stats + delete-all.

These pin the response contract the frontend's InventoryTable depends on:
``items`` array + ``total`` count is the inventory table's "rows visible /
rows matching" pair; facets back the filter dropdown counts; stats backs
the dashboard counter that used to silently truncate at the page cap.
"""

from __future__ import annotations


def _create_vm(client, **overrides):
    payload = {
        "name": "vm-default",
        "source_hostname": "vm-default.local",
    }
    payload.update(overrides)
    r = client.post("/api/vms", json=payload)
    assert r.status_code == 201, r.json()
    return r.json()


# ---------------------------------------------------------------------------
# Response shape
# ---------------------------------------------------------------------------
def test_list_returns_wrapped_response_with_total(client):
    for i in range(3):
        _create_vm(client, name=f"vm-{i}", source_hostname=f"vm-{i}.local")
    body = client.get("/api/vms").json()
    assert set(body.keys()) >= {"items", "total", "skip", "limit"}
    assert body["total"] == 3
    assert len(body["items"]) == 3


def test_skip_alias_offset_back_compat(client):
    for i in range(5):
        _create_vm(client, name=f"vm-{i}", source_hostname=f"vm-{i}.local")
    skip_body = client.get("/api/vms?skip=2&limit=2&sort_by=name").json()
    offset_body = client.get("/api/vms?offset=2&limit=2&sort_by=name").json()
    assert skip_body["items"] == offset_body["items"]
    assert skip_body["skip"] == 2 == offset_body["skip"]


# ---------------------------------------------------------------------------
# Sorting
# ---------------------------------------------------------------------------
def test_sort_by_name_ascending(client):
    for name in ["charlie", "alpha", "bravo"]:
        _create_vm(client, name=name, source_hostname=f"{name}.local")
    body = client.get("/api/vms?sort_by=name&sort_order=asc").json()
    assert [v["name"] for v in body["items"]] == ["alpha", "bravo", "charlie"]


def test_sort_by_name_descending(client):
    for name in ["charlie", "alpha", "bravo"]:
        _create_vm(client, name=name, source_hostname=f"{name}.local")
    body = client.get("/api/vms?sort_by=name&sort_order=desc").json()
    assert [v["name"] for v in body["items"]] == ["charlie", "bravo", "alpha"]


def test_sort_by_environment_with_id_tiebreaker(client):
    # Two VMs share environment=prod; the stable-tie-break on id should
    # keep their order consistent across calls.
    _create_vm(client, name="a", source_hostname="a.local", environment="prod")
    _create_vm(client, name="b", source_hostname="b.local", environment="prod")
    _create_vm(client, name="c", source_hostname="c.local", environment="dev")
    body = client.get("/api/vms?sort_by=environment&sort_order=asc").json()
    envs = [v["environment"] for v in body["items"]]
    # dev, prod, prod — within the prod group, id-ascending => a then b.
    assert envs == ["dev", "prod", "prod"]
    names = [v["name"] for v in body["items"]]
    assert names == ["c", "a", "b"]


def test_invalid_sort_column_rejected(client):
    r = client.get("/api/vms?sort_by=notes")
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------
def test_filter_by_multi_value_status(client):
    a = _create_vm(client, name="a", source_hostname="a.local")
    b = _create_vm(client, name="b", source_hostname="b.local")
    _create_vm(client, name="c", source_hostname="c.local")
    # Promote two of three to a non-default status so we have something
    # to filter on.
    client.patch(f"/api/vms/{a['id']}", json={"status": "baseline_captured"})
    client.patch(f"/api/vms/{b['id']}", json={"status": "validated"})

    body = client.get(
        "/api/vms?status=baseline_captured&status=validated&sort_by=name"
    ).json()
    assert body["total"] == 2
    assert {v["name"] for v in body["items"]} == {"a", "b"}


def test_filter_by_environment(client):
    _create_vm(client, name="p1", source_hostname="p1.local", environment="prod")
    _create_vm(client, name="p2", source_hostname="p2.local", environment="prod")
    _create_vm(client, name="s1", source_hostname="s1.local", environment="staging")
    body = client.get("/api/vms?environment=prod").json()
    assert body["total"] == 2


def test_filter_by_os_family(client):
    _create_vm(client, name="r1", source_hostname="r1.local", os_family="rhel")
    _create_vm(client, name="w1", source_hostname="w1.local", os_family="windows")
    body = client.get("/api/vms?os_family=rhel").json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "r1"


def test_search_matches_name_owner_app_hint(client):
    _create_vm(client, name="payroll-db", source_hostname="x.local", owner="finance-team")
    _create_vm(client, name="hr-app-01", source_hostname="y.local", application_hint="payroll-svc")
    _create_vm(client, name="other", source_hostname="z.local")
    # "payroll" hits the name on one row, the app_hint on the other.
    body = client.get("/api/vms?search=payroll&sort_by=name").json()
    assert body["total"] == 2
    assert {v["name"] for v in body["items"]} == {"payroll-db", "hr-app-01"}


def test_filters_combine_with_and_semantics(client):
    _create_vm(
        client, name="match",
        source_hostname="match.local", environment="prod", os_family="rhel",
    )
    _create_vm(
        client, name="only_env",
        source_hostname="o1.local", environment="prod", os_family="windows",
    )
    _create_vm(
        client, name="only_os",
        source_hostname="o2.local", environment="dev", os_family="rhel",
    )
    body = client.get("/api/vms?environment=prod&os_family=rhel").json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "match"


# ---------------------------------------------------------------------------
# Pagination math
# ---------------------------------------------------------------------------
def test_pagination_total_independent_of_page_size(client):
    for i in range(7):
        _create_vm(client, name=f"vm-{i:02d}", source_hostname=f"vm-{i}.local")
    page1 = client.get("/api/vms?skip=0&limit=3&sort_by=name").json()
    page2 = client.get("/api/vms?skip=3&limit=3&sort_by=name").json()
    page3 = client.get("/api/vms?skip=6&limit=3&sort_by=name").json()
    assert page1["total"] == page2["total"] == page3["total"] == 7
    assert len(page1["items"]) == 3
    assert len(page2["items"]) == 3
    assert len(page3["items"]) == 1  # last page partial
    names = [v["name"] for v in page1["items"] + page2["items"] + page3["items"]]
    # No duplication across pages, all 7 covered exactly once.
    assert len(names) == len(set(names)) == 7


# ---------------------------------------------------------------------------
# Facets
# ---------------------------------------------------------------------------
def test_facets_count_by_dimension(client):
    _create_vm(client, name="a1", source_hostname="a1", environment="prod", os_family="rhel")
    _create_vm(client, name="a2", source_hostname="a2", environment="prod", os_family="rhel")
    _create_vm(client, name="a3", source_hostname="a3", environment="staging", os_family="windows")

    body = client.get("/api/vms/facets").json()
    assert body["total"] == 3
    assert body["environment"]["prod"] == 2
    assert body["environment"]["staging"] == 1
    assert body["os_family"]["rhel"] == 2
    assert body["os_family"]["windows"] == 1
    # status is always populated (defaults to "discovered" for new rows)
    assert body["status"]["discovered"] == 3


def test_facets_respect_active_filters(client):
    _create_vm(client, name="p-rhel", source_hostname="p1", environment="prod", os_family="rhel")
    _create_vm(client, name="p-win", source_hostname="p2", environment="prod", os_family="windows")
    _create_vm(client, name="s-rhel", source_hostname="s1", environment="staging", os_family="rhel")

    # With environment=prod applied, os_family facets only reflect prod VMs.
    body = client.get("/api/vms/facets?environment=prod").json()
    assert body["total"] == 2
    assert body["os_family"] == {"rhel": 1, "windows": 1}


def test_facets_skip_null_values(client):
    _create_vm(client, name="a", source_hostname="a")  # environment=None
    _create_vm(client, name="b", source_hostname="b", environment="prod")
    body = client.get("/api/vms/facets").json()
    # None doesn't leak into the facet keys
    assert "" not in body["environment"]
    assert None not in body["environment"]
    assert body["environment"] == {"prod": 1}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def test_stats_returns_total_and_by_status(client):
    a = _create_vm(client, name="a", source_hostname="a.local")
    _create_vm(client, name="b", source_hostname="b.local")
    client.patch(f"/api/vms/{a['id']}", json={"status": "validated"})

    body = client.get("/api/vms/stats").json()
    assert body["total"] == 2
    assert body["by_status"]["discovered"] == 1
    assert body["by_status"]["validated"] == 1


def test_stats_total_unaffected_by_pagination(client):
    # The dashboard counter regression: stats must NOT depend on a
    # page-size query parameter.
    for i in range(250):
        _create_vm(client, name=f"vm-{i:03d}", source_hostname=f"vm-{i}.local")
    body = client.get("/api/vms/stats").json()
    assert body["total"] == 250


# ---------------------------------------------------------------------------
# DELETE /api/vms/all
# ---------------------------------------------------------------------------
def test_delete_all_requires_confirm(client):
    _create_vm(client, name="a", source_hostname="a.local")
    r = client.delete("/api/vms/all")
    assert r.status_code == 400
    # The VM survives the rejected call.
    assert client.get("/api/vms").json()["total"] == 1


def test_delete_all_with_confirm_clears_inventory(client):
    for i in range(5):
        _create_vm(client, name=f"vm-{i}", source_hostname=f"vm-{i}.local")
    r = client.delete("/api/vms/all?confirm=true")
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 5
    assert client.get("/api/vms").json()["total"] == 0


def test_delete_all_with_filter_scoped(client):
    _create_vm(client, name="p1", source_hostname="p1", environment="prod")
    _create_vm(client, name="p2", source_hostname="p2", environment="prod")
    _create_vm(client, name="s1", source_hostname="s1", environment="staging")
    r = client.delete("/api/vms/all?confirm=true&environment=prod")
    assert r.status_code == 200
    assert r.json()["deleted_count"] == 2
    # Only the staging row survives.
    survivors = client.get("/api/vms").json()
    assert survivors["total"] == 1
    assert survivors["items"][0]["name"] == "s1"


def test_delete_all_records_summary_audit(client):
    _create_vm(client, name="a", source_hostname="a")
    _create_vm(client, name="b", source_hostname="b")
    client.delete("/api/vms/all?confirm=true")
    audit = client.get("/api/audit?action=vm.delete_all&limit=10").json()
    assert len(audit) >= 1
    entry = next(e for e in audit if e["action"] == "vm.delete_all")
    assert entry["details"]["deleted_count"] == 2


def test_delete_all_cascades_snapshots(client):
    vm = _create_vm(client, name="a", source_hostname="a.local", ssh_user="root")
    # Drop in a snapshot manually so we can confirm it cascades.
    client.post(
        f"/api/vms/{vm['id']}/snapshots",
        json={"ssh_user": "root", "raw_data": {"meta": {}}, "checksum": "abc"},
    )
    snaps_before = client.get(f"/api/vms/{vm['id']}/snapshots").json()
    assert len(snaps_before) == 1

    r = client.delete("/api/vms/all?confirm=true")
    assert r.json()["deleted_count"] == 1
    # The VM is gone; the snapshot listing returns 404 for the now-deleted VM.
    r = client.get(f"/api/vms/{vm['id']}/snapshots")
    assert r.status_code == 404
