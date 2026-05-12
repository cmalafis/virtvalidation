# Inventory Page Guide

The inventory tab is VirtValidate's primary VM management surface.
On a 1,000-VM federal fleet it must paginate, filter, sort, and
support bulk operations without dragging the dashboard to a halt.
This document covers the operator-facing workflow plus the API
contract that backs it.

---

## At a glance

| Capability             | Shortcut |
|------------------------|----------|
| Filter by status / env / OS / app / vCenter | dropdowns in filter bar |
| Free-text search       | name / owner / application_hint |
| Sort                   | click any column header |
| Paginate               | controls below the table (25/50/100/250) |
| Bulk delete            | row checkboxes → Delete Selected |
| Bulk import            | links to `/rvtools/upload` |
| Wipe inventory         | **Delete All** with `DELETE ALL` confirmation |
| Per-row actions        | capture baseline, edit, delete |
| Shareable URL          | every filter+sort+page is reflected in `?…` |

---

## Filtering & sorting

The filter bar above the table exposes five dropdowns plus a
search input. All dropdowns are multi-select. Filters compose with
AND semantics — selecting `environment=prod` and `os_family=rhel`
returns VMs that are both prod **and** RHEL.

Facet counts in each dropdown reflect the **current filter set
minus that dimension's own selection**. So clicking "Environment:
prod" then opening the OS dropdown shows OS counts within prod
only — making it obvious how a new filter would narrow the result.

The search input matches case-insensitively against:

- `name`
- `owner`
- `application_hint`

A 300ms debounce keeps the backend from being hit on every
keystroke.

Sorting: click a column header to sort by that column. Click
again to reverse. The current sort is shown by a `▲` / `▼` glyph
next to the header. Sorts are stable — VMs with identical sort
keys keep a consistent order across pages (tie-break on internal
id).

---

## Pagination

The pager exposes:

- **Page size selector** — 25 / 50 / 100 / 250. Default 50.
  Larger pages render slower; >250 needs UI virtualization (a
  deferred item in [`docs/SCALE.md`](./SCALE.md)).
- **Page navigator** — first / prev / next / last + "Page X of Y"
- **Result count** — "Showing 51–100 of 1,247 · environment: prod"

Limits and defaults live in
[`docs/LIMITS.md`](./LIMITS.md) (`DEFAULT_PAGE_SIZE`,
`MAX_PAGE_SIZE`).

---

## Bulk operations

Select rows with the checkboxes. The bulk action bar appears once
at least one row is selected and offers:

- **Delete Selected** — bulk delete via the existing
  `BulkDeleteVMsModal` flow (one audit row per VM, see
  `docs/SECURITY.md`).
- **Clear** — drops the selection.

Capture-baseline-in-bulk and run-validation-in-bulk live on the
separate `/capture-baselines` and `/validate-batch` pages —
inventory's bulk bar links to those when scope > one page.

---

## Delete All (testing + reset)

The **Delete All** button on the page actions row clears every
VM matching the current filter (or every VM in inventory if no
filter is set). It triggers a confirmation modal that:

1. Shows the resolved scope: "Delete all 1,247 VMs in inventory"
   or "Delete 247 filtered VMs?"
2. Lists the filter fingerprint so operators know exactly what
   they're wiping.
3. Requires typing `DELETE ALL` to enable the destroy button.

Under the hood it calls `DELETE /api/vms/all?confirm=true&<filters>`.
The endpoint refuses without `?confirm=true` and records a single
summary audit row containing the filters and deleted count. The
cascade deletes baselines + validations + snapshots in the same
transaction.

For federal customers running a tabletop wipe (lab reset between
exercises), the operator's preferred sequence is:

```bash
# Authoritative wipe via the API, audited.
curl -X DELETE 'http://localhost:8000/api/vms/all?confirm=true' \
     -H 'x-actor: tabletop-operator'
```

Don't `TRUNCATE` the database directly — the audit trail won't
record the operation and post-exercise compliance review will
flag the gap.

---

## URL state

Every filter / sort / page is reflected in the URL search string:

```
/inventory?status=production&sortBy=name&sortOrder=asc&page=3
```

Reload, share, or bookmark the URL — the table comes back up in
the same state.

---

## API contract

The inventory table is backed by these endpoints. They live under
`backend/app/api/vms.py` and are pinned by
`backend/tests/test_inventory_pagination.py`.

### `GET /api/vms` — paginated listing

Query parameters:

| Param            | Type            | Default          | Notes |
|------------------|-----------------|------------------|-------|
| `skip`           | int             | 0                | items to skip |
| `offset`         | int             | (alias for skip) | back-compat |
| `limit`          | int             | 50               | ≤ `MAX_PAGE_SIZE` |
| `sort_by`        | enum            | `name`           | name / status / environment / os_family / application_hint / source_vcenter_id / created_at / updated_at |
| `sort_order`     | `asc` \| `desc` | `asc`            | |
| `status`         | enum (multi)    | —                | discovered / baseline_captured / migrated / validated / failed |
| `vcenter_source_id` | int (multi)  | —                | |
| `environment`    | str (multi)     | —                | |
| `application_hint` | str (multi)  | —                | |
| `os_family`      | str (multi)     | —                | |
| `classification_level` | str (multi) | —             | joins through vCenter |
| `search`         | str             | —                | name / owner / app_hint contains |

Response:

```json
{
  "items": [...VMRead...],
  "total": 1247,
  "skip": 100,
  "limit": 50
}
```

### `GET /api/vms/facets` — per-dimension counts

Same filter params as `GET /api/vms`. Returns:

```json
{
  "status":               {"discovered": 800, "validated": 200},
  "environment":          {"prod": 600, "staging": 250, "dev": 150},
  "os_family":            {"rhel": 700, "windows": 280, "ubuntu": 20},
  "application_hint":     {"epic-emr-prod": 60, "athena": 32},
  "vcenter_source_id":    {"1": 334, "2": 333, "3": 333},
  "classification_level": {"unclassified": 999, "secret": 2},
  "total": 1000
}
```

Powers the dropdown counts. NULL values are omitted (a VM with
no `environment` doesn't show as a key in the environment facet).

### `GET /api/vms/stats` — cheap fleet counters

No filter params. Returns:

```json
{
  "total": 1247,
  "by_status": {"discovered": 800, "validated": 200, "failed": 47, "baseline_captured": 200},
  "missing_from_last_upload": 12
}
```

The dashboard header tile reads from this so the "Total VMs" count
stays accurate without fetching every row. Cheap because every
query is a `COUNT(*) GROUP BY status` — no row reads.

### `DELETE /api/vms/all` — bulk-clear with filters

Query parameters: `confirm` (required true) + same filter params
as `GET /api/vms`.

Response:

```json
{"deleted_count": 247}
```

Records a single `vm.delete_all` audit row with the filter
fingerprint and count. Cascades through ORM relationships so
baselines + validations get cleaned in the same transaction.

---

## Performance notes

- **Indexes** — the hot filter paths are covered by composite
  indexes (`source_vcenter_id` + `status`, `application_hint` +
  `environment`). See `backend/app/models/vm.py.__table_args__`.
- **Page sizes** — pages of 250 take ~150-200ms on Postgres for a
  10K fleet. The 250 cap is set in the frontend dropdown — the
  backend supports up to `MAX_PAGE_SIZE=1000` for scripted clients.
- **Facet caching** — facet queries are NOT cached today. They
  hit the DB on every filter change. If federal customers report
  slow filter UX on 50K-VM fleets, the next step is a short-TTL
  in-memory cache keyed by the filter querystring.
- **Search debounce** — 300ms. Don't lower it below 150ms or you
  start hammering the backend on rapid typists.

---

## Adding a new filter dimension

1. Add the column to `app/models/vm.py` (with migration).
2. Add the field to `app/schemas/vm.py` `VMBase` if it should
   round-trip on `GET /api/vms/{id}`.
3. Extend `_apply_vm_filters` in `app/api/vms.py` to accept the
   new dimension.
4. Expose it as a multi-value `Query(...)` param on `list_vms`,
   `vm_facets`, and `delete_all_vms`.
5. Add a key to `VMFacetsResponse` so the frontend's dropdown can
   read counts.
6. Add a `FacetDropdown` to `frontend/src/components/InventoryTable.jsx`.
7. Pin the new filter with a test in
   `tests/test_inventory_pagination.py`.

The matched `_apply_vm_filters` call ensures the same filter set
behaves identically across list, facets, stats, and delete-all —
keep them in sync when adding new dimensions.

---

## Deferred

- **Saved filter views** — a "remember this filter combo" feature
  is on the roadmap but out of scope for the initial pagination
  refactor.
- **Export to formats other than CSV** — current export dumps the
  visible page only. A full filtered-export endpoint is a future
  enhancement.
- **Inline editing** — operators currently use the per-row ✎ to
  open the EnrollVMsModal; in-place editing would speed up bulk
  metadata cleanup but adds modal/state complexity.
- **Real-time inventory updates** — when a capture or validation
  changes a VM's status, the inventory table doesn't auto-refresh
  beyond the explicit Refresh button. Server-sent events would
  fix this; tracked alongside the categorization streaming work.
