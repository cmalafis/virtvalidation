# Scale & Capacity

VirtValidate scales by tier. Each tier corresponds to a deployment
shape and an LLM backend choice. Pick the tier that matches your
fleet size; upgrade tiers as the fleet grows.

This document is honest about **tested** vs **designed-for** capacity.
Federal customers will probe both numbers — don't claim untested
scale.

---

## Tested capacity

These numbers come from real test runs against the appliance, not
theoretical analysis.

| Dimension                       | Tested ceiling | Notes |
|---------------------------------|----------------|-------|
| Single vCenter                  | 1,000 VMs      | RVTools auto-link upload validated end-to-end in <1s on UBI/SQLite TestClient; ~2-5s with real Postgres + network round-trip. |
| Multiple vCenters               | 5 sources × 1,000 VMs each = 5,000 total | Tested with delta uploads weekly over a 4-week window. Auto-link upload routes per-VM by the file's vCenter column. |
| Single RVTools upload (auto-link) | 1,000 VMs across 3 vCenters | Parse + auto-match + multi-vCenter import; budget 60s, observed 0.5-2s. No chunking required at this scale. See `docs/RVTOOLS_GUIDE.md` for the timing table. |
| Bulk POST body cap                | 10,000 VMs per request | Centralized in `app.core.limits` — `MAX_VMS_PER_BULK_CREATE` / `MAX_VMS_PER_BULK_DELETE` / `MAX_VMS_PER_RVTOOLS_IMPORT`. Env-var overridable for very large fleets. See `docs/LIMITS.md`. |
| Inventory paginated read          | 1,000 rows per page | `MAX_PAGE_SIZE` cap; default first-render uses 50 (`DEFAULT_PAGE_SIZE`). |
| Concurrent baseline captures    | 25             | Above this, BackgroundTasks queue; no failures, just slower. |
| Concurrent validation runs      | 10             | Each holds a DB session + an SSH session + an LLM call. |
| Inventory list page render      | 1,000 rows     | No virtualization yet — beyond this the page slows perceptibly. |
| Level 1 categorization batch    | 10 VMs / call (default; configurable) | Sized so the prompt fits Llama 3 8B's 8192 context with response headroom. See `docs/LLM_TUNING.md`. |
| Level 1 full vCenter            | 57 VMs in 6 batches sequential ≈ 22 min on Ollama llama3:8b | RHOAI/KServe is faster (parallel batches) but untested at full scale. |
| Hierarchical plan generation    | 1,000 VMs (mocked LLM, unit-test); 57 VMs (live, Ollama) | Chunker is deterministic & sub-second; per-chunk LLM time scales linearly with chunk count. |
| Hierarchical chunks per plan    | 30+ chunks for 1,000 VMs (Llama backend, max_size=15) | Each chunk ≤ backend's `max_planning_chunk_size`. |

## Designed-for capacity (not yet tested)

The data model and APIs are built for these numbers. The bottleneck
is the LLM throughput; KServe / vLLM with horizontal replicas
unblocks it.

| Dimension                       | Designed-for | Requires |
|---------------------------------|--------------|----------|
| Total fleet across all vCenters | 50,000 VMs   | KServe with ≥4 vLLM replicas, PostgreSQL HA, BackgroundTask queue per replica. |
| Concurrent vCenter sources      | 50           | No technical ceiling; UI listing has no virtualization above ~30. |
| Categorization throughput       | 200 batches/min | KServe with N replicas → ~N batches in parallel. |
| Inventory list page             | 50,000 rows  | Needs react-window virtualization (deferred — see below). |

---

## Scaling tiers

### Tier 1 — Standalone Appliance (≤ 1,000 VMs)

| Component | Configuration |
|-----------|---------------|
| Deployment | `podman-compose up -d` on a single host |
| LLM backend | Ollama, llama3:8b |
| Database | Single Postgres container |
| Concurrent captures | Up to 25 (BackgroundTask thread pool) |
| Storage | Local volumes or NFS |

Sweet spot: dev / test / single-tenant federal lab. Captures, validations,
and categorization all run on a single host. The appliance is
stateful — backup the Postgres + SSH-keys volumes per
[deploy/README.md](../deploy/README.md).

**Recommended limit settings:** defaults are correct (no
override). 10K bulk caps cover the largest reasonable single
upload. See [`docs/LIMITS.md`](./LIMITS.md).

### Tier 2 — OpenShift + Ollama (≤ 5,000 VMs)

| Component | Configuration |
|-----------|---------------|
| Deployment | Helm chart, `llm.backend=ollama` |
| LLM backend | In-cluster Ollama Deployment, model PVC |
| Database | StatefulSet Postgres with backed-up PVC |
| Concurrent captures | 50 (multiple replicas of backend) |
| Storage | StorageClass with snapshot support |

Sweet spot: single large enterprise. Ollama is the operational
simplicity choice — one model, one PVC, no platform team needed.
At 5,000 VMs the categorizer takes ~30 minutes; the operator runs it
weekly.

**Recommended limit settings:** defaults. The 10K bulk caps fit
in one HTTP request; categorization itself runs in chunks of 10
internally.

### Tier 3 — OpenShift + KServe (≤ 50,000 VMs)

| Component | Configuration |
|-----------|---------------|
| Deployment | Helm chart, `llm.backend=kserve` |
| LLM backend | KServe InferenceService with ≥4 vLLM replicas |
| Database | PostgreSQL HA (Patroni / Cloud SQL / RDS) |
| Concurrent captures | 100+ |
| Storage | KMS-encrypted CSI |

Sweet spot: federal customer with RHOAI, multi-vCenter inventory,
weekly delta uploads. The KServe runtime gives the categorizer real
parallelism — the same 50,000-VM run that takes hours on Tier 2
finishes in 10–15 minutes.

**Recommended limit settings:** raise bulk caps to 25,000 so
operators can upload a full vCenter delta in one request. Keep
`MAX_PAGE_SIZE` at 1,000 (UI lacks virtualization). Example:

```env
MAX_VMS_PER_BULK_CREATE=25000
MAX_VMS_PER_BULK_DELETE=25000
MAX_VMS_PER_BULK_ACTION=25000
MAX_VMS_PER_PLAN_SCOPE=25000
MAX_VMS_PER_RVTOOLS_IMPORT=25000
```

Beyond 25K per single request, split the RVTools export per
vCenter — see [`docs/LIMITS.md`](./LIMITS.md) for the rationale.

### Upgrade triggers

When to move up a tier:

| Symptom | Likely tier | Move to |
|---------|-------------|---------|
| Backend pod OOM during categorization | Tier 1, single-host RAM exhausted | Tier 2 (split DB / Ollama / backend) |
| Categorization takes longer than the operator's coffee break | Tier 2 with Ollama at concurrency=1 | Tier 3 (KServe + vLLM replicas) |
| Inventory page slow to load | Any | Wait for UI virtualization (deferred work — see below) or paginate aggressively in your filters |
| Capture throughput plateaued | Any | Add backend replicas; check NetworkPolicy egress rules aren't bottlenecking SSH egress |

---

## Database tuning for large deployments

PostgreSQL defaults assume a single-tenant workload. For a
50,000-VM fleet with weekly RVTools uploads:

```ini
# postgresql.conf knobs that matter
shared_buffers = 4GB                # 25% of RAM
effective_cache_size = 12GB         # 75% of RAM
work_mem = 64MB                     # per-sort scratch — Level 1 GROUP BY uses this
maintenance_work_mem = 512MB        # vacuum / index build
max_connections = 200               # backend pool + admin headroom
```

The schema ships with the indexes we expect on the hot paths:

  - `ix_vms_source_vcenter_status` — inventory list filtered by vCenter
  - `ix_vms_target_namespace`      — target-cluster assignment views
  - `ix_vms_app_env`               — Level 1 grouping aggregation
  - `ix_snapshots_vm_collected_at` — latest-snapshot lookups
  - `ix_groups_vcenter_kind`       — group listing per vCenter

Verify with `EXPLAIN (ANALYZE, BUFFERS) SELECT …` on your slow
queries; if the planner ignores an index, consider raising
`random_page_cost` so it prefers index scans on warm caches.

### Read replicas

Not implemented today. The dashboard runs every read against the
primary. For Tier 3+, plan to:

  1. Add a read-only `DATABASE_URL_RO` env var.
  2. Route `/api/vms` (the dashboard's hottest endpoint) at
     `db_ro` instead of `db`.
  3. Keep writes on the primary.

This is on the roadmap as ``read-replica-routing``.

### Snapshot table partitioning

Baseline snapshots accumulate at 1 row per VM per scheduled capture
(twice daily by default → 730 rows/year/VM → 36M rows for a 50K
fleet over a year). Partition by month if your size warrants:

```sql
ALTER TABLE baseline_snapshots
  PARTITION BY RANGE (collected_at);
```

We don't ship this as the default because partitioning a small
fleet creates more operational toil than it saves. Reach for it
when `pg_total_relation_size('baseline_snapshots')` exceeds ~50 GB.

---

## Deferred work

These items are designed-for but not implemented. Don't promise
them to customers without explicitly noting they're future work.

### Hierarchical plan-generation pipeline (shipped)

The plan generator now runs a hierarchical pipeline (Python chunker
→ per-chunk LLM → Python assembly → cross-chunk review LLM).
See `docs/PLANNING_ARCHITECTURE.md` for the full design.

Per-backend chunk-size recommendations (`max_planning_chunk_size`):

  - **Ollama / Llama 3 8B** — 15 VMs/chunk, sequential. A 1K-VM plan
    runs ~70 chunks at ~3 min/chunk = ~3.5 hours. Acceptable for
    overnight runs; not ideal for interactive use.
  - **KServe / RHOAI** — 50 VMs/chunk, 3-way parallel. Same 1K-VM
    plan runs ~20 chunks in ~7 parallel batches = ~15 min.
  - **vLLM (large context)** — 75 VMs/chunk, 5-way parallel. Same
    plan in ~5 min.

These numbers are validated against the chunker's unit tests; live
end-to-end timing on a 1K-VM fleet hasn't been measured yet
(scheduled for the RHOAI demo cluster's first full run).

### Multi-program coordination (deferred)

Cross-program / cross-appliance migration coordination ("two
programs sharing a CMDB") needs a new `MigrationProgram`-level
sequencing engine. Not on the v1.0 roadmap. The single-program
hierarchical pipeline above covers customer-tier migrations; multi-
program coordination is a v1.5+ concern.

### ~~UI virtualization~~ — resolved by server-side pagination

Previously: the inventory table rendered every row and took >1s to
first paint beyond ~1,000 rows, and the fix was assumed to be
`react-window` / `@tanstack/react-virtual`.

The PatternFly rebuild made that moot. The table pages server-side
(20 rows by default) against `GET /api/vms` with `skip`/`limit`, so
the row count in the DOM is bounded regardless of fleet size, and
filter counts come from `GET /api/vms/facets` rather than from
counting loaded rows. Virtualization is only worth revisiting if a
future view genuinely needs an unpaginated list.

### Multi-program switcher UI

`MigrationProgram` rows can already coexist (East Coast / West
Coast). Migration Plans lists every plan, but nothing scopes the UI
to one program; switching between them needs a masthead program
picker + scoped queries. Tagged
``multi-program-switcher``.

### Real-time progressive streaming

Categorization status today polls every 3s. KServe-backed Tier 3
will benefit from server-sent events so the UI fills in
batch-by-batch. Tagged ``progressive-streaming``.

### RVTools schema validation against multiple versions

Current parser handles RVTools 4.x. Older 3.x exports work
best-effort. Tagged ``rvtools-3x-validation``.

### Email / Slack notifications on long-running tasks

Categorization on a 50K-VM fleet takes 15+ minutes on Tier 3 and
hours on Tier 1. Notifying the operator when complete is on the
roadmap; today the operator polls. Tagged ``async-completion-notify``.

---

## Honesty notes for federal sales conversations

  - **"Tested up to 5K VMs across 5 vCenters."** — true.
  - **"Designed for 50K VMs."** — true; needs Tier 3.
  - **"Streams Level 1 progress in real time."** — partial — the
    UI polls every 3s. Server-sent events for true streaming is
    deferred.
  - **"Multi-program migration management."** — partial — the data
    model supports it; the UI ships with single-program views.
  - **"FIPS-compliant when host OS is in FIPS mode."** — true. See
    [FIPS_DEPLOYMENT.md](FIPS_DEPLOYMENT.md).
  - **"Air-gapped by design."** — true with Ollama (Tier 1/2). Tier
    3 KServe deployments depend on the operator's KServe being
    air-gapped too.
