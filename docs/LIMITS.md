# Limits + Capacity Knobs

Every bulk-operation cap and pagination ceiling in VirtValidate
lives in **`backend/app/core/limits.py`**. The constants there are
the single source of truth; nowhere else in the codebase hardcodes
the values. Every constant is overridable via an environment
variable.

This document explains what each limit is for, what the default is,
and when an operator should consider changing it.

---

## Quick reference

| Constant | Env var | Default | What it guards |
|----------|---------|--------:|----------------|
| `DEFAULT_PAGE_SIZE` | `DEFAULT_PAGE_SIZE` | 50 | first-render page size on listing endpoints |
| `MAX_PAGE_SIZE` | `MAX_PAGE_SIZE` | 1,000 | upper bound on `?limit=` query params |
| `MAX_VMS_PER_BULK_CREATE` | `MAX_VMS_PER_BULK_CREATE` | 10,000 | `POST /api/vms/bulk` body length |
| `MAX_VMS_PER_BULK_DELETE` | `MAX_VMS_PER_BULK_DELETE` | 10,000 | `DELETE /api/vms` body length |
| `MAX_VMS_PER_BULK_ACTION` | `MAX_VMS_PER_BULK_ACTION` | 10,000 | bulk capture / validation / schedule scopes |
| `MAX_VMS_PER_PLAN_SCOPE` | `MAX_VMS_PER_PLAN_SCOPE` | 10,000 | plan generation `scope.vm_ids` |
| `MAX_VMS_PER_RVTOOLS_IMPORT` | `MAX_VMS_PER_RVTOOLS_IMPORT` | 10,000 | RVTools auto-link upload payload |
| `MAX_VMS_PER_CHUNK` | `MAX_VMS_PER_CHUNK` | 500 | recommended chunk size for streaming flows |
| `MAX_AUDIT_LOG_PAGE_SIZE` | `MAX_AUDIT_LOG_PAGE_SIZE` | 500 | audit listing `?limit=` cap |

A bad value (non-integer or non-positive) logs a startup warning
and falls back to the default — startup must stay robust against
misconfigured ConfigMaps.

---

## Why these defaults

### Bulk operations: 10,000

Federal customer fleets routinely hit 5,000-10,000 VMs in a single
inventory. A v0.1.x cap of 500 silently 422'd any 1K+ RVTools
upload, and the UI hung at "IMPORTING..." because the error never
surfaced. The 10K ceiling covers every customer-segment we've
encountered while still rejecting obvious request-size abuse
(a 100K-VM payload is ~50 MB of JSON and warrants chunking).

If a customer's fleet exceeds 10K VMs in one vCenter, **chunk
through the auto-link upload flow** — split the RVTools export
into multiple files and upload sequentially. The per-VM cost is
unchanged; the operator just gets more progress indicators.

### Pagination: 50 default / 1,000 max

The dashboard's first render uses 50 to keep the page paint under
~200ms. Power users and scripted clients can pass `?limit=1000`
to grab a whole vCenter in one round-trip. The ceiling exists so
a bug in a paging loop can't OOM the worker.

The inventory page itself has no virtualization yet — beyond ~1,000
rows render time degrades. See the `ui-virtualization-inventory`
tag in `docs/SCALE.md`.

### Audit log: 500

The audit-log UI has no virtualization at all. The cap stays low
so the timeline page renders predictably. Going above 500 makes
the render time-quadratic.

### Chunk size: 500

Not currently enforced on any endpoint — chunked uploads were
deferred per spec since the 10K monolithic endpoint covers
everything we've shipped. The constant exists so an eventual
streaming-importer has a tested default; pick chunk sizes around
500 to balance JSON-parse overhead vs network round-trips.

---

## When to adjust

### Smaller deployments (lab, single-vCenter, <500 VMs)

No adjustment needed — the defaults cover orders of magnitude more.
Lowering the limits doesn't save resources; the cap only fires
when a request exceeds it.

### Federal classified deployment (FedRAMP High, IL5+)

Defaults are correct. If your customer has documented per-request
size limits in their authorization (e.g., "no API payload may
exceed 5 MB"), lower the bulk caps so payloads stay under their
ceiling:

```env
# Roughly 5 MB of VMs at ~500 bytes per row
MAX_VMS_PER_BULK_CREATE=5000
MAX_VMS_PER_RVTOOLS_IMPORT=5000
```

### Very large customer (15K+ VMs per vCenter)

Bump the caps in the `.env` / ConfigMap:

```env
MAX_VMS_PER_BULK_CREATE=25000
MAX_VMS_PER_RVTOOLS_IMPORT=25000
MAX_VMS_PER_BULK_ACTION=25000
```

Beyond ~25K per single request you'll start hitting nginx's
default `client_max_body_size` (32M in our config) and FastAPI's
JSON-parse budget. At that scale split the upload into multiple
RVTools files per vCenter — the auto-link flow handles each
independently.

### Performance-sensitive read paths

The pagination defaults trade first-render latency against caller
convenience. If your customer reports slow inventory loads, drop
`DEFAULT_PAGE_SIZE` to 25 — first render gets faster, "Load more"
stays one click away.

---

## Performance implications

| Endpoint | Bottleneck | Caps that matter |
|----------|-----------|------------------|
| `POST /api/vms/bulk` | Postgres INSERT throughput (~5-10K/s) | `MAX_VMS_PER_BULK_CREATE` |
| `POST /api/rvtools/upload-multi-vcenter` | Per-vCenter delta diff (cheap), then Postgres INSERT | `MAX_VMS_PER_RVTOOLS_IMPORT` |
| `POST /api/snapshots/capture-bulk` | SSH connection pool (10 parallel) | `MAX_VMS_PER_BULK_ACTION` + `max_parallel` |
| `POST /api/validations/run-bulk` | LLM throughput (Tier 3 only) | `MAX_VMS_PER_BULK_ACTION` + tier classifier |
| `GET /api/vms` | Postgres index scan (fast) | `MAX_PAGE_SIZE` |
| `GET /api/audit` | Postgres index scan + UI render | `MAX_AUDIT_LOG_PAGE_SIZE` |

A 1,000-VM RVTools upload completes end-to-end in **0.5-2 seconds**
on real hardware (parse + auto-match + multi-vCenter import). The
budget the spec set was 60 seconds; we have two orders of magnitude
of headroom for typical scales.

---

## How to override at runtime

The constants are read at module import time, so changes require a
backend restart:

```bash
# Edit .env or set in podman-compose.yml / Helm values
echo "MAX_VMS_PER_BULK_CREATE=25000" >> .env

# Restart the backend
podman-compose restart backend
# or
kubectl -n virtvalidate rollout restart deployment/backend
```

Verify with:

```bash
curl -s http://localhost:8000/api/health/full | jq
```

The health endpoint doesn't currently expose the resolved limits.
Inspect by importing the module:

```bash
podman exec virtvalidation_backend_1 python -c \
    "from app.core.limits import *; print(MAX_VMS_PER_BULK_CREATE)"
```

---

## Frontend error surfacing

When a request exceeds a cap, the backend returns a Pydantic 422
with `detail: [{loc: ["body", "vms"], msg: "List should have at
most N items", ...}]`.

The shared `frontend/src/utils/fetchJSON.js` flattens that into
`"vms: List should have at most N items"` so the UI can display
specific field-level errors instead of `[object Object]`. The
`RVToolsUpload` page renders the first 5 lines in an
`ImportErrorBanner` with a **Try again** action — the UI never
gets stuck in an indefinite "importing" state.

---

## Related docs

- [`docs/SCALE.md`](./SCALE.md) — tested capacity, scaling tiers,
  end-to-end timing.
- [`docs/RVTOOLS_GUIDE.md`](./RVTOOLS_GUIDE.md) — upload flow.
- [`docs/VALIDATION_SCALING.md`](./VALIDATION_SCALING.md) — bulk
  validation throughput.
