# RVTools Upload Guide

VirtValidate's primary inventory ingest path is **RVTools** — the
free vSphere reporting tool that exports VM lists as `.xlsx`. This
guide covers the workflows that work well at scale (per-vCenter
uploads, weekly delta refreshes) and the patterns that don't.

---

## Recommended workflow: per-vCenter uploads

For multi-vCenter customers, register each vCenter as a source
**before** uploading RVTools, then attach each upload to its
vCenter. The benefits compound:

  - **Isolation.** VMs are scoped to their vCenter — same VM name
    across vCenters is fine (treated as different VMs).
  - **Delta detection.** The system compares the incoming RVTools
    against the existing inventory **for that vCenter only**. New /
    updated / removed buckets render in a preview before commit.
  - **Federal classification.** Each vCenter carries a
    `classification_level` (unclassified / CUI / SECRET /
    top_secret). Uploads inherit that scope.
  - **Default mappings.** The vCenter row carries
    `default_target_namespace` and `default_target_storage_class`,
    so VMs imported under it pick up sensible OCP-Virt defaults
    without per-VM configuration.

### Registering a vCenter

From the UI: **Sources → vCenters → + Register vCenter**.

From the API:

```bash
curl -X POST http://<host>:8000/api/sources/vcenters \
  -H "Content-Type: application/json" \
  -d '{
    "name": "vc-east",
    "hostname": "vc-east.corp.local",
    "region": "us-east",
    "site": "dc-iad",
    "classification_level": "cui",
    "default_target_namespace": "openshift-mtv-east",
    "default_target_storage_class": "ocs-storagecluster-ceph-rbd"
  }'
```

### Auto-link upload (recommended)

The top-level **Upload RVTools** page (`/rvtools/upload`) reads the
vCenter column on every vInfo row and routes each VM to its source
automatically. No more "pick a vCenter then upload" — multi-vCenter
files distribute correctly in one shot.

Flow:

  1. **Pick file.** The browser parses the `.xlsx` and groups VMs by
     the value in the **vCenter** column.
  2. **Auto-match.** The frontend hits
     `POST /api/sources/vcenters/auto-match` with the detected
     hostnames. The backend matches them to registered
     `VCenterSource` rows:
       - **exact** — normalized hostname equality
         (`vc-east-01.corp.local.` == `VC-EAST-01.CORP.LOCAL`)
       - **fuzzy** — prefix match either direction
         (registered short `vc-east-01` matches RVTools FQDN
         `vc-east-01.corp.local`)
  3. **Operator reviews matches.** For each detected hostname the
     operator chooses:
       - Match to an existing vCenter (auto-filled when matched)
       - Create a new vCenter (hostname pre-filled)
       - Skip these VMs
  4. **Per-vCenter preview + import.** A single call to
     `POST /api/rvtools/upload-multi-vcenter` runs the delta-import
     per vCenter and returns counts for each. Partial-success is
     the default contract: a per-vCenter failure surfaces in the
     `errors` list but doesn't roll back the others.

The page is at **Sources → vCenters → ⬆ Upload RVTools**, or
directly at `/rvtools/upload`.

#### Legacy per-vCenter upload

Each vCenter row on the **Sources → vCenters** page has an **Upload
RVTools** action. The flow is three-stage and stays inside one modal
so the operator never loses context:

1. **Parse client-side.** The browser reads the workbook, pulls the
   `vInfo` sheet (RVTools' canonical VM list), and runs every row
   through `frontend/src/utils/parseRVTools.js` — the same parser
   the global Enroll modal uses, extracted so both flows stay
   consistent. Extra sheets are ignored.
2. **Preview the delta.** The modal submits the parsed list to
   `POST /api/sources/vcenters/{id}/rvtools/preview`. The response
   buckets every VM into:
   - **new** — name not in this vCenter's existing VMs
   - **updated** — name matches but a tracked field changed
   - **removed** — VM exists in inventory but not in the upload
   - **unchanged** — exact match
3. **Confirm and commit.** The operator clicks **Confirm Import**;
   the modal fires `POST /api/sources/vcenters/{id}/rvtools/import`
   with the same payload. Behavior:
   - **Created** rows land with `source_vcenter_id` set and
     `status='discovered'`.
   - **Updated** rows get the changed tracked fields applied;
     non-tracked state (target_namespace, owner-set notes, etc.) is
     preserved.
   - **Removed** rows are **flagged** with
     `missing_from_last_upload=True` rather than deleted. Operators
     decommission separately when ready — a deliberate two-step that
     mirrors `DELETE /api/sources/vcenters/{id}` to avoid silent
     fleet wipes.

#### Sync vs async imports

Imports of fewer than 500 VMs run in-line and return the counts
directly. Above that threshold the endpoint returns 202 with a
`task_id` and the modal polls
`GET /api/sources/vcenters/{id}/rvtools/import/{task_id}` until
completion. The threshold is `ASYNC_IMPORT_THRESHOLD` in
`backend/app/core/rvtools_import.py`.

#### Idempotency

Re-running the same import is safe. Already-present rows count as
`unchanged`; previously-marked-missing rows that reappear flip
`missing_from_last_upload` back to `False` and emit a
`vm.rvtools_import.reappeared` audit row.

#### What gets audited

One audit row per material change:

- `vm.rvtools_import.create` — new VM enrolled.
- `vm.rvtools_import.update` — tracked fields changed (the
  `details.changes` carries the per-field diff).
- `vm.rvtools_import.marked_missing` — VM absent from this upload
  but present in prior imports.
- `vm.rvtools_import.reappeared` — VM came back after being marked
  missing.
- `vcenter.rvtools_import_triggered` — single per-call event with
  the source / VM count for high-level reconciliation.

Filter the trail with `GET /api/audit?action=vm.rvtools_import.update`
when reconciling a specific upload.

### Upload-flow comparison

| Flow | When to use | Persists `source_vcenter_id`? |
|------|-------------|-------------------------------|
| **Discover → Virtual Machines → Import VMs** (recommended) | Any RVTools file, single or multi-vCenter | ✅ Yes — auto-detected per VM, with a routing step you confirm |
| **Configure → vCenter Sources → row → Import** | Legacy compat, or when the file lacks a vCenter column | ✅ Yes — operator-specified |

The import page covers everything the per-vCenter flow does plus
multi-vCenter distribution. It shows every detected vCenter hostname and
lets you route each to a registered source; VMs whose hostname routes
nowhere are **skipped, not guessed at**, and the count is shown before you
commit. The per-vCenter flow is kept for files without a vCenter column
and for operators who want explicit control.

> The old "+ Add VMs" enroll modal in the top nav is gone as of the
> PatternFly UI. Its non-scoped import path landed VMs ungrouped, which
> then had to be fixed up by hand; routing them at import time is
> strictly better.

### How the import runs (server-side, since 2026-09)

The browser uploads the file and polls; it does not parse.
`POST /api/imports/rvtools` (multipart) spools the upload and returns
`202` with a job. The job moves through:

```
uploaded → scanning → awaiting_mapping → importing → completed | failed | cancelled
```

- **scanning** counts VM rows per vCenter hostname found in the file.
- **awaiting_mapping** is the routing step — nothing has been written yet.
  An upload that already carries `vcenter_mapping` / `default_vcenter_id`
  skips it.
- **importing** streams the workbook (openpyxl read-only mode) and upserts
  in batches of 500, one transaction per batch. A failure or a cancel keeps
  the batches already committed; uploading the same file again finishes the
  job without duplicates.

Sheets read: `vInfo` (required), and `vDisk`, `vSnapshot`, `vNetwork`,
`vCPU`, `vMemory` when present. Use RVTools' **Export all to Excel** — a
`vInfo`-only export imports fine, but disk-level migration blockers (RDM,
independent and shared disks) can't be assessed from it.

**Identity.** A VM is matched within its vCenter by **MoRef** (`VM ID`
column) when the export has one, otherwise by name. A VM renamed in vSphere
keeps its row. VM names are *not* unique — vSphere only requires uniqueness
per folder.

**Modes.** `upsert` (default) updates changed fields and flags VMs absent
from the file as "no longer in export" (never deletes). `create_only` adds
new VMs and leaves existing rows untouched. An empty cell never blanks
existing data, and an environment label set by hand is never overwritten.

**Problem rows** never abort the import. `GET /api/imports/{id}/rejects`
(and `/rejects.csv`) lists each with sheet, row and reason:

| Outcome | Examples |
|---|---|
| `rejected` — not imported | missing VM name; name over 255 chars (names are identity, never truncated); template; SRM placeholder; duplicate of an earlier row |
| `warning` — imported, flagged | no datastore could be determined; no network listed |

`.xls` is refused with a message — re-save as `.xlsx`. Upload size is capped
by `MAX_IMPORT_UPLOAD_BYTES` (100 MB), VM rows by
`MAX_VMS_PER_RVTOOLS_IMPORT` (10,000). One import runs at a time.

### Tested capacity

Measured 2026-09-20, Postgres 16, through the real API, fixture from
`scripts/generate-test-rvtools.py` (all six sheets):

| File | Source rows | First import | Re-import (all unchanged) |
|---|---:|---:|---:|
| 1,000 VMs / 3 vCenters (`tests/fixtures/rvtools-1000.xlsx`) | 7,409 | 1.2 s | 1.5 s |
| 5,000 VMs / 4 vCenters | 37,126 | 6.5 s | 8.2 s |

Parser alone on the 1,000-VM file: 1.8 s, 8 MB peak. Whole-process peak RSS
for the 5,000-VM run: ~310 MB (the API process, not just the parser).

### Generating test files

`scripts/generate-test-rvtools.py` produces realistic RVTools XLSX
exports for scale testing:

```bash
python3 scripts/generate-test-rvtools.py \
    --vm-count 1000 \
    --vcenter-count 3 \
    --output tests/fixtures/rvtools-1000.xlsx
```

Output mirrors the production vInfo column set and distributes VMs
across federal-customer-shaped applications (EHR, PACS, billing,
identity, dev sandbox) + environments (prod/staging/dev) + OS
families (RHEL 9 / RHEL 8 / Win2022 / Win2019 / Ubuntu 22).

---

## Exporting from RVTools

From the RVTools application:

1. **File → Export to xlsx all.**
2. Pick a directory; RVTools writes `RVTools_export_<timestamp>.xlsx`.
3. **Verify the `vInfo` sheet has the columns** VirtValidate uses:
   - `VM` (name)
   - `Powerstate`
   - `OS according to the configuration file`
   - `Primary IP Address`
   - `Folder`
   - `Annotation`
   - `vSphere Networks` (comma-separated)
   - `vSphere Datastores` (comma-separated)
   - Any custom attributes you maintain (Owner, Environment,
     Application, Business Unit). VirtValidate maps these into
     the corresponding fields when the column name matches one of
     the recognized aliases listed in `app/core/rvtools_columns.py`.
4. Don't post-process the workbook — sorting is fine, but inserting
   columns or merging cells confuses the parser.

### Versions

| RVTools version | Status | Notes |
|-----------------|--------|-------|
| 4.x (current)   | ✅ Tested | The canonical target. |
| 3.x             | ⚠ Best-effort | Most columns parse; older sheets may be missing `vSphere Networks` |
| 2.x and older   | ❌ Unsupported | Recommend upgrading RVTools first. |

---

## Multi-vCenter customer patterns

### Pattern A — One Region per vCenter

```
vc-east   → 2,400 VMs (US East data centers)
vc-west   → 1,800 VMs (US West data centers)
vc-emea   → 600 VMs (EU data center)
```

Register each vCenter, upload weekly. The inventory list filters by
vCenter so operators see only the scope they own.

### Pattern B — Classification boundaries

```
vc-unclass    classification_level=unclassified  (4,000 VMs)
vc-cui        classification_level=cui           (1,200 VMs)
vc-secret     classification_level=secret        (300 VMs)
```

Each vCenter has its own RVTools export and credential set. The
appliance keeps them isolated; cross-vCenter migration plans require
explicit operator confirmation.

### Pattern C — Per-application vCenter (rare)

Some customers run a vCenter per app suite:

```
vc-epic-emr      → 800 VMs
vc-cerner-img    → 400 VMs
```

Works fine, but Level 1 categorization typically discovers the same
application boundaries from naming patterns alone — so the per-app
vCenter is mostly an organizational choice, not a requirement.

---

## Delta uploads — the weekly refresh pattern

Federal migration projects run for months. Customers re-export
RVTools weekly and re-upload to surface inventory drift. The delta
preview is the load-bearing surface here.

### Typical weekly cycle

1. Friday COB: export RVTools for each vCenter.
2. Upload via the dashboard. Review the delta preview:
   - **Few new / few updated** — normal weekly drift, commit.
   - **Many new** — likely a new VM cluster came online, double-check
     before committing so the planner doesn't suddenly include them
     in the next wave.
   - **Many removed** — likely a VM cluster was decommissioned, OR
     RVTools missed the export from a connected vCenter (verify
     the export ran on the right host).
3. Commit. The audit log captures the upload size + per-bucket
   counts so federal reviewers can reconstruct the inventory at any
   point.

### What "updated" means

The delta engine tracks these fields and flags any change:

  - `source_hostname`, `ip_address`
  - `os_family`, `role`, `environment`, `owner`, `application_hint`
  - `vsphere_networks`, `vsphere_datastores`

Other fields (notes, target mappings, SSH config) are ignored — they're
operator-edited inside VirtValidate, not sourced from RVTools.

### List ordering doesn't trigger updates

RVTools sometimes reshuffles the order of VM networks between
exports. The delta engine compares list fields **order-insensitive**
so a reshuffle alone doesn't flood the review queue with non-changes.

---

## Fallback: single-file RVTools without registered vCenter

If you upload RVTools without selecting a vCenter, the appliance
treats the entire file as a single source. This works but loses:

  - Cross-vCenter isolation (every VM goes into one bucket).
  - Per-vCenter delta detection (subsequent uploads compare
    against the entire ungrouped inventory).
  - Default target mappings (no per-vCenter defaults).

Use this only for proof-of-concept demos. Production deployments
should register vCenters first.

> **Future work — tagged ``rvtools-vcenter-inference``** — when an
> operator uploads a single file with multiple folder roots or
> custom attributes that suggest distinct vCenters, prompt them to
> split the upload across registered vCenters. Today there's no
> auto-inference; operators must split manually.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Delta preview returns 404 | Wrong `vcenter_id` in URL | Check `/api/sources/vcenters` for the right id |
| Many VMs flagged "updated" but nothing meaningful changed | Tracked field shape changed across RVTools versions | Inspect the `diff` payload — empty diffs shouldn't happen, file an issue |
| Upload commits but inventory list shows no new VMs | Source-vcenter-id wasn't set on commit payload | Frontend sends `source_vcenter_id` per VM; backend rejects bulk requests without it |
| `vInfo` sheet missing | RVTools 3.x or earlier | Upgrade RVTools, or re-run with the `--all` flag |
| Excel export has Chinese / Japanese characters in column names | RVTools language setting | Switch RVTools UI to English before exporting; column-name aliasing is en-US only |
| 10,000+ VMs in one upload | Hitting the API request limit | Split the upload by folder or environment — payload limit is 10,000 VMs per request |
| Encoding errors (BOM, weird characters) | Non-UTF-8 export | Re-export from RVTools 4.x — older versions write Latin-1 encoded files |

---

## Security note: RVTools exports contain inventory metadata

The `.xlsx` files include hostnames, IP addresses, OS info, custom
attributes, and folder paths. For federal customers:

  - Treat RVTools exports as **CUI** at minimum (sometimes higher
    depending on the environment).
  - Don't store exports in unrestricted locations.
  - Upload directly through the appliance — VirtValidate parses
    client-side, so the file never touches an intermediate server.
  - The appliance audit log captures only the upload metadata
    (size, row counts), not the file contents — operators wanting
    full chain-of-custody should checksum the file locally before
    upload.
