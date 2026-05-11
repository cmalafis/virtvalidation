# Validation Scaling

Post-migration validation runs at scale — federal customer fleets
range from 100 to 10,000 VMs. Running an LLM call against every VM
is wasteful (most migrations are clean) and expensive (token cost
adds up fast on a 10K-VM cutover). VirtValidate's validation
pipeline routes each VM through the cheapest tier that can produce
a defensible verdict.

This document explains the architecture, the operator-visible
knobs, and how to tune the cost/quality balance.

---

## Three tiers, evaluated in order

```
┌──────────┐   ┌──────────┐   ┌──────────┐   ┌─────────────┐
│ SSH      │ → │ Python   │ → │ Cache    │ → │ LLM         │
│ collect  │   │ classify │   │ lookup   │   │ analysis    │
└──────────┘   └────┬─────┘   └────┬─────┘   └─────────────┘
                    │              │
                    ▼              ▼
              Tier 1 / 2     Tier 3 hit
              ~30 ms/VM      ~5 ms/VM
                                            Tier 3 miss
                                            ~30-120 s/VM
```

| Tier | What | Latency | LLM call? |
|------|------|--------:|----------:|
| **Tier 1** | Empty or trivial diff | ~30 ms | No |
| **Tier 2** | Diff matches deterministic rules | ~30 ms | No |
| **Tier 3 (cached)** | Tier 3 diff matches a recent LLM verdict | ~5 ms | No |
| **Tier 3 (fresh)** | Ambiguous diff, cache miss | ~30-120 s | Yes |

**Expected distribution on a well-executed migration:**

  - Tier 1 — 50-60% (a clean migration produces no material drift)
  - Tier 2 — 15-30% (kubevirt-agent appears, eth0 renames, etc.)
  - Tier 3 cached — 5-15% (homogeneous fleets get cache hits)
  - Tier 3 fresh — 5-15% (genuinely novel diffs that need reasoning)

For a 1,000-VM fleet that means ~50-150 LLM calls instead of 1,000.

---

## Tier 1 — Trivial / empty diffs

The structured diff (computed in Python from baseline vs current
state) is checked for material change before anything else:

  - Every diff sub-section is empty → Tier 1 pass.
  - Network changes are pure rename (eth0 → ens192 with same IPs)
    → Tier 1 pass. The known rename table in
    ``app.core.validation_tiers.KNOWN_NETWORK_RENAMES`` lists the
    pairs covered.

The Tier 1 verdict is hand-written ("No material drift detected.")
with `confidence: high` — there's nothing for the LLM to add.

---

## Tier 2 — Deterministic rules

When Tier 1 doesn't match, the rule pass runs:

  - **Critical service stopped on production VM → fail.**
    `CRITICAL_SERVICES_FOR_PRODUCTION` covers web (nginx, httpd,
    apache2), databases (postgresql, mysql, mariadb, mongod, redis),
    container runtimes (docker, containerd, podman), and identity
    (named, sssd, smbd). Production scope (`environment in
    {"prod","production"}`) escalates to `fail`; non-prod to `warn`.
    Either way, no LLM call — the operator's next step is unambiguous.

  - **Only known-good additions → pass.**
    `KNOWN_GOOD_NEW_SERVICES` covers kubevirt-agent,
    qemu-guest-agent, cloud-init, virt-who. When the entire diff
    is these services appearing (and nothing else changed), the
    migration is clean — pass without an LLM call.

  - **Anything else → fall through to Tier 3.**

The rule lists are *operator-tunable* knobs:

```python
# backend/app/core/validation_tiers.py
KNOWN_GOOD_NEW_SERVICES: frozenset[str] = frozenset({"kubevirt-agent", ...})
CRITICAL_SERVICES_FOR_PRODUCTION: frozenset[str] = frozenset({"postgresql", ...})
KNOWN_NETWORK_RENAMES: list[tuple[str, str]] = [("eth0", "ens192"), ...]
```

When a customer's fleet has its own custom systemd unit names,
update these constants and re-deploy. The tier classifier picks up
the new rules without code changes elsewhere.

---

## Tier 3 — LLM analysis (with caching)

When the diff is genuinely ambiguous, the pipeline:

1. **Hashes the diff.** SHA-256 of canonical-JSON-encoded
   ``(diff, os_family)``. The OS family is part of the key because
   the same "service X stopped" diff means different remediation
   on Linux vs Windows.

2. **Checks the cache.** `validation_llm_cache` table — TTL 7 days.
   On hit, the cached verdict is returned with `cached: true`
   stamped on the audit row. Hit count is incremented for the
   admin dashboard.

3. **On miss, calls the LLM.** The verdict is persisted into the
   cache for future hits. The structured validator (see
   ``docs/PROMPT_ENGINEERING.md``) ensures the verdict has every
   required field; if not, a single retry-with-feedback is
   attempted before falling back to a "manual review needed"
   verdict that doesn't poison the cache.

### Why 7 days?

Long enough to cover a single weekend cutover wave + the following
week of validation runs. Short enough that stale verdicts age out
before the next migration cycle. The TTL is configurable per call
but defaults match the typical federal cutover cadence.

### When the cache is wrong

Cache hits include `cached: true` in the audit log. If an operator
sees a verdict that doesn't match the current state, they can
invalidate the cache via:

```bash
DELETE FROM validation_llm_cache WHERE cache_key = '<key>';
```

Or for a clean reset:

```bash
DELETE FROM validation_llm_cache;
```

---

## Bulk operations

The validation UI's *Validate batch* page hits two endpoints:

  - `POST /api/validations/preview-tiers` — SSH-collects every VM
    in scope, runs the classifier, and reports
    `{tier1: N, tier2: N, tier3: N, estimated_llm_calls: N}`. No
    LLM call. Operators see the expected token cost before
    submission.

  - `POST /api/validations/run-bulk` — spawns a BackgroundTask
    that walks the scope, routes each VM through tiers, and
    reports per-VM status (queued / running / completed / failed)
    via `GET /api/validations/run-bulk/{task_id}`.

Per-VM failures don't abort the bulk run; they surface as
`status: failed` rows with an `error` field.

---

## Baseline capture at scale

Mirrored on the capture side: `POST /api/snapshots/capture-bulk`
takes the same scope filter and runs the existing `collect_and_store`
pipeline against each VM under two concurrency caps:

  - **Global cap** (`max_parallel`, default 10) — total SSH sockets
    in flight across the whole run.
  - **Per-vCenter cap** (`per_vcenter_parallel`, default 5) — one
    vCenter source can't starve another.

The orchestrator is in `app.core.bulk_capture` and uses
`asyncio.Semaphore` to enforce both caps. **Baseline capture never
calls the LLM** — it's pure SSH-collect-and-persist. The
``run_bulk_capture`` code path is audited to ensure no LLM import
or method call appears anywhere on its branch.

---

## Scheduled validation

`ValidationSchedule` rows store cron expressions + scope filters.
The `app.core.scheduler` module reads them at startup and fires the
bulk-validation pipeline on each cron tick.

Common patterns:

  - **"Validate all production every 24 hours"** —
    `cron: "0 2 * * *"`, scope: `{environment: "prod"}`.
  - **"Validate the recently-migrated fleet"** — scope:
    `{vm_ids: [...]}` for one specific wave. Useful in the 24-72h
    post-cutover window.

CRUD is via `/api/validation-schedules`. Operators fire schedules
manually with `POST /api/validation-schedules/{id}/fire` —
identical code path to the cron tick, so testing a schedule
doesn't require waiting for its window.

---

## Cost + usage metrics

`GET /api/system/llm-usage?hours=24` returns:

```json
{
  "by_operation": {
    "validation": {"calls": 12, "input_tokens": 36000, "output_tokens": 14000}
  },
  "totals": {
    "input_tokens": 36000,
    "output_tokens": 14000,
    "estimated_cost_usd": 0.00,
    "rate_per_1m_input": 0.0,
    "rate_per_1m_output": 0.0
  },
  "validations": {
    "total": 47,
    "tier_distribution": {"tier1": 25, "tier2": 10, "tier3": 12},
    "cached_count": 5,
    "cache_hit_rate_percent": 10.6,
    "needs_manual_review_count": 0
  },
  "cache": {
    "entries": 142,
    "total_hits": 87,
    "by_os_family": {"rhel-like": {"entries": 130, "hits": 80}, "windows": {"entries": 12, "hits": 7}}
  }
}
```

### Configuring per-token cost

Set `LLM_COST_PER_MILLION_INPUT_TOKENS` and
`LLM_COST_PER_MILLION_OUTPUT_TOKENS` in `.env` to surface USD
estimates. Defaults to 0 because the standalone-appliance Ollama
backend has zero marginal cost — set non-zero values when running
against KServe/vLLM on shared infrastructure.

---

## Tuning playbook

### "LLM calls too expensive"

  - Confirm tier distribution skew is correct via
    `GET /api/system/llm-usage`. If Tier 3 share is >40%,
    something's off:
      - Many false-positive "diffs" — likely a noisy collector
        category. Review the `_diff_*` helpers in
        ``app.core.llm.client``.
      - Real fleet-wide drift — talk to the migration team; the
        chunker / mapping is producing inconsistent outputs.
  - Bump cache TTL if cutovers are tightly grouped — e.g., 14 days
    for a 2-week wave window.
  - Add custom services to `KNOWN_GOOD_NEW_SERVICES` /
    `CRITICAL_SERVICES_FOR_PRODUCTION` so Tier 2 covers the
    customer's specific stack.

### "Tier 1 is too aggressive"

  - Operator wants the LLM to comment even on clean migrations.
    Tier 1 is hardcoded to skip the LLM when the diff is empty —
    this is by design. If you need a "validate-with-narrative-only"
    mode, that's a separate `validation.narrative-only` feature
    not currently shipped.

### "Bulk capture is too slow"

  - Raise `max_parallel` (default 10) — the bottleneck is usually
    SSH socket count, not VirtValidate CPU.
  - Raise `per_vcenter_parallel` (default 5) only if you have one
    high-VM vCenter and other vCenters are idle.

### "Bulk validation is too slow"

  - Tier 3 calls serialize on a single-stream Ollama. KServe with
    parallel replicas (3+) processes Tier 3 in parallel — bulk
    validation throughput tracks `LLMBackend.max_concurrent_calls`.

---

## Related docs

- [`docs/PROMPT_ENGINEERING.md`](./PROMPT_ENGINEERING.md) —
  validation system-prompt structure + rationale per section.
- [`docs/LLM_TUNING.md`](./LLM_TUNING.md) — Ollama context window,
  batch sizes, retry behavior.
- [`docs/SCALE.md`](./SCALE.md) — overall fleet-scale guidance.
