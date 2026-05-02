# Scale & Capacity

VirtValidate scales by tier. Each tier corresponds to a deployment
shape and an LLM backend choice. Pick the tier that matches your
fleet size; upgrade tiers as the fleet grows.

This document is honest about **tested** vs **designed-for** capacity.
Federal customers will probe both numbers — don't claim untested
scale.

---

## Tested capacity (v0.1.x)

These numbers come from real test runs against the appliance, not
theoretical analysis.

| Dimension                       | Tested ceiling | Notes |
|---------------------------------|----------------|-------|
| Single vCenter                  | 1,000 VMs      | RVTools upload + bulk enrollment confirmed. |
| Multiple vCenters               | 5 sources × 1,000 VMs each = 5,000 total | Tested with delta uploads weekly over a 4-week window. |
| Concurrent baseline captures    | 25             | Above this, BackgroundTasks queue; no failures, just slower. |
| Concurrent validation runs      | 10             | Each holds a DB session + an SSH session + an LLM call. |
| Inventory list page render      | 1,000 rows     | No virtualization yet — beyond this the page slows perceptibly. |
| Level 1 categorization batch    | 200 VMs / call | Larger batches risk LLM context exhaustion + tail-of-list hallucinations. |
| Level 1 full vCenter            | 5,000 VMs in ~50 batches sequential = 25 min on Ollama llama3:8b | KServe with vLLM is faster but untested at full scale. |

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

### Hierarchical planner — Levels 2 + 3

The categorizer (Level 1) ships in v0.x. The full hierarchical
planner (Levels 2 strategy + Level 3 wave detail) is targeted for
v1.0:

  - **Level 2** — single LLM call per program, sees group summaries
    (not individual VMs), emits campaign + sequencing structure.
    Persists into `migration_programs.strategy`.
  - **Level 3** — one LLM call per campaign with full VM context,
    emits MTV-shaped wave assignments. Persists into a new
    `campaign_waves` table (schema not yet shipped).
  - **Streaming UI** — Level 1 progress already polls every 3s.
    Level 2/3 will benefit from server-sent events so the UI fills
    in as each campaign completes.

The data model (`MigrationProgram`) is shipped now so the migration
to a streaming-aware UI doesn't need a schema break later. The
categorizer's batched LLM-call infrastructure is the template for
Level 3's per-campaign calls — expect the same shape, different
prompts.

### UI virtualization

The inventory table renders every row. Beyond ~1,000 rows the page
takes >1s to first paint. Fix is `react-window` or
`@tanstack/react-virtual` — reasonably small change but needs
careful testing against the existing keyboard navigation +
multi-select bulk-action affordances. Tagged
``ui-virtualization-inventory``.

### Multi-program switcher UI

`MigrationProgram` rows can already coexist (East Coast / West
Coast). The dashboard surfaces the latest plan; switching between
programs needs a top-bar program picker + scoped queries. Tagged
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
