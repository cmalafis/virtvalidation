# Planning Architecture

VirtValidate has two complementary planning paths. Both follow the
same architectural principle — **mechanical pre-processing in Python
before any LLM call** — but at different scales and triggered by
different APIs:

| Path | Entry point | LLM input size | Use case |
|------|-------------|----------------|----------|
| **Two-stage** (synchronous) | `POST /api/plans` | M ≤ `LLM_MAX_ITEMS_PER_CALL` groups (default 20) | Default. Quick plan generation on ≤ 1,000 VMs. |
| **Hierarchical** (async) | `POST /api/plans/generate` | Multiple per-chunk + 1 review call | Large fleets (>1K VMs) with strategy / mapping context. |

If you're touching the planner, read **both** sections — they share
the core "mechanical pre-processing in Python before any LLM call"
principle, but the integrity invariants live at different layers.

---

## Two-stage path — mechanical pre-classification + group-based LLM

### Stages

```
POST /api/plans  →  MigrationPlanner.plan_with_groups()
                       │
                       ▼
  Stage 1 — PreClassifier (Python, deterministic)
    Partition by vCenter + target namespace, sub-group by
    application_hint + role, then network/datastore overlap,
    then name-prefix. Cap M ≤ 20 by consolidating smallest.
                       │
                       ▼
  Stage 2 — LLM wave assignment (small input)
    Prompt: "Here are 8 pre-formed groups. Assign each to a
    migration wave (1-N) by dependency + risk."
    Output: group_ids per wave — never raw vm_ids.
                       │
                       ▼
  Stage 3 — Mechanical expansion
    Expand group_ids → vm_ids via the Stage 1 group table.
    Deterministic, bug-free. Integrity checks pass by
    construction.
```

### Why this architecture

Before pre-classification, the v0.1.x planner fed raw VM lists to
the LLM and asked it to group + order in one shot. That failed at
10+ VMs across every model tested in May 2026:

- Llama 3.2 3B dropped vm_ids past 20.
- Granite 3.1 8B failed at 57 (the customer fleet that triggered
  this refactor).
- Symptoms were silent — fluent JSON output that omitted vm_ids,
  duplicated ids across waves, or invented ids that weren't in
  the input.

The fix wasn't a bigger model — it was to stop asking the LLM to
categorize. Mechanical grouping is something Python can do
deterministically + auditably. The LLM should only see the small
number of *groups* it has to *order*.

See `CLAUDE.md` → "LLM Input Discipline (Architectural Rule)" for
the design rule this implements.

### What the pre-classifier groups by

In priority order:

1. **PRIMARY (must match)** — `source_vcenter_id`, `target_namespace`.
   Different vCenters or different destination namespaces never merge.
2. **SECONDARY (strong cohesion)** — `application_hint` (operator-
   supplied) sub-split by detected role, then network ∩ datastore
   overlap (≥1 of each).
3. **TERTIARY (fallback)** — name-prefix bucket
   (e.g. `web-prod-*` → one group).

Roles are heuristic: name patterns like `db|postgres|mongo` → "data",
`web|nginx|apache` → "web", `app|api|svc` → "app", and so on. State
follows role (data + infrastructure → stateful; web/app/edge →
stateless). Risk follows both (stateful+data → high, stateless+web
small group → low).

### Output shape

Each group exposes:

- `id` — deterministic composite string
  (`vc1/prod/data/stateful/hint:epic-emr:data`).
- `vm_ids` — sorted list.
- `role`, `state`, `migration_risk` — heuristic tags.
- `depends_on` — seeded hints (app groups depend on data + infra).
- `shared_attributes` — networks/datastores/hints that made the
  group cohere.
- `notes` — operator-readable one-liner.

The plan response surfaces these in `groups[]` so operators can see
exactly how their VMs were clustered before wave assignment.

### `POST /api/plans/preview-groups`

Same Stage 1 logic, no Stage 2 LLM call, no plan persisted. Lets
operators preview the grouping before paying the LLM round-trip.
Useful for debugging ("why is X in the same group as Y?") and for
demonstrating the value of mechanical grouping during customer
demos.

### Wave assignment reliability — retry + mechanical fallback

The Stage-2 LLM call is the only step that involves non-deterministic
output. Smaller models (Llama 3.2 3B, Granite 3.1 8B) sometimes
return invalid wave assignments — duplicating a group_id across
waves, dropping one entirely, or violating the JSON schema. The
planner is built to handle this gracefully:

  1. **Strict prompt** — the system prompt opens with `HARD
     CONSTRAINTS` and lists the five non-negotiable rules: every
     group_id appears in EXACTLY ONE wave, no omissions, no
     duplicates, sequential wave numbers, self-verify before
     responding. Constraint repetition before AND after the wave-
     ordering principles measurably reduces the LLM's failure rate
     on small models.
  2. **Retry with corrective context** — when validation rejects an
     LLM response, the planner re-prompts with the validation error
     prepended: *"PREVIOUS ATTEMPT FAILED with this error: …"*. The
     LLM can self-correct given the specific failure mode. Default
     is 3 attempts (configurable via `max_llm_attempts`).
  3. **Mechanical fallback** — when every retry fails, the planner
     produces a wave assignment via deterministic topological sort
     over `dependency_hints` + role priority
     (`infrastructure → data → app → web → edge → other`). The
     plan is less nuanced than the LLM's (rationale comes from
     template, not bespoke prose) but it's always valid.

The mechanical fallback is **not a failure mode** — it's an
architectural choice. Federal customers must always get a plan;
they should never see a 502 from `POST /api/plans` because the LLM
had a bad day. Plans generated mechanically are just as valid as
LLM-generated plans, just less nuanced.

The plan response carries a `method` field so operators can see
which path produced the plan:

  - `"llm"` — first attempt succeeded.
  - `"llm_retry_1"` / `"llm_retry_2"` — succeeded on retry N.
    Flags an under-performing model; consider an upgrade if these
    show up often in audit logs.
  - `"mechanical_fallback"` — every LLM attempt failed; the
    deterministic assigner produced this plan. The summary string
    includes the last LLM error for debugging.

See `tests/test_wave_assignment_robustness.py` for the pinned
contracts: corrective-context propagation, deterministic fallback,
configurable `max_llm_attempts`, cycle handling in
`_topological_sort`.

### Required VM metadata

The preclassifier reads these fields off the `VM` model — each
contributes to the cohesion signal. Sparse metadata produces more,
thinner groups; richly populated metadata produces fewer, richer
groups.

| Field                          | Cohesion role                                      | What happens if missing |
|--------------------------------|----------------------------------------------------|--------------------------|
| `source_vcenter_id`            | PRIMARY partition — never merged across            | All VMs land in the "unknown vcenter" partition |
| `target_namespace`             | PRIMARY partition                                  | Falls back to "default" target namespace |
| `application_hint`             | Strongest in-partition cohesion (with role split)  | Falls back to network/datastore overlap |
| `role` (operator-supplied)     | Overrides name-pattern role detection              | Falls back to name pattern matching |
| `vsphere_networks`             | Secondary cohesion (network ∩ datastore)           | Used only when present |
| `vsphere_datastores`           | Secondary cohesion                                 | Used only when present |
| `environment`                  | Reported on group as a shared_attribute            | Group's shared_attributes shows empty `environments` |
| `os_family`                    | Reported on group                                  | Empty `os_families` |

**Deferred enrichment:** the VM model doesn't yet carry `cluster`,
`host`, `folder`, `resource_pool` columns that the RVTools export
includes. Tracked in the roadmap — adding them would let the
preclassifier produce even richer groups (e.g. "all VMs in the
prod-us-east-cluster-01 / /prod/ehr-pro folder"). For now, the
operator-supplied `application_hint` carries that signal.

### Example: rich preclassification output

For the DHA fleet (see `docs/TEST_DATA.md`), the preclassifier
produces output like:

```json
{
  "id": "vc1/ehrpro-prod/data/stateful/hint:ehrpro:data",
  "vm_count": 3,
  "role": "data",
  "state": "stateful",
  "risk": "high",
  "depends_on": [],
  "shared_attributes": {
    "networks": ["VLAN-120-Data-Prod"],
    "datastores": ["prod-gold-ssd-01"],
    "application_hints": ["ehrpro"],
    "environments": ["production"],
    "os_families": ["rhel"]
  },
  "notes": "share networks: VLAN-120-Data-Prod; share datastores: prod-gold-ssd-01; application_hint: ehrpro; environment: production"
}
```

Every cohesion signal is populated → the LLM sees a tight, well-
described group → wave ordering is easy.

### Opt-out: `preclassification_enabled: false`

`PlanCreate` accepts this flag to fall back to the legacy raw-VM
flow. Use it for:

- Testing model behavior on small fleets (the legacy path was the
  shape every model trained on).
- Single-VM plans where one VM per group is fine.

Federal customers should leave it enabled — the audit trail benefits
from the deterministic grouping.

---

# Hierarchical Chunking (large-fleet path)

The async pipeline below kicks in for strategy-driven generations
through `POST /api/plans/generate`. It's a different code path from
the two-stage synchronous flow above, sized for inventories that
exceed the synchronous path's working set.

---

## Why we chunk in Python before the LLM

The single-shot prompt the v0.1.x planner used hit three failure modes
as customer scope grew:

  1. **Context overflow.** Past ~200 VMs the prompt exceeds Llama 3
     8B's 8K window. Ollama silently truncates, the model produces
     a fluent JSON wave plan that *omits* the back half of the input,
     and the operator never knows.
  2. **Attention drift.** Long before the context formal limit,
     models lose attention on tail items in long lists. Even a
     150-VM prompt that *fits* produces noticeably worse rationales
     for VMs at the bottom.
  3. **Time-to-first-byte.** A 2K-VM single-shot call on local Ollama
     takes 8+ minutes regardless of how much of the inventory the
     model actually reasons about. Operators see a frozen progress
     bar and assume something's wrong.

Python chunking solves all three: each LLM call sees ≤ 50 VMs (model-
sized), prompts stay short and well within context, and per-chunk
calls overlap with progress reporting so the UI shows real motion.

---

## Pipeline stages

```
┌──────────┐     ┌──────────────┐     ┌──────────┐     ┌──────────┐
│ Python   │     │   LLM × N    │     │ Python   │     │  LLM × 1 │
│ chunker  │ ──▶ │ (per-chunk)  │ ──▶ │ assembly │ ──▶ │  review  │
└──────────┘     └──────────────┘     └──────────┘     └──────────┘
   ~100ms           5-15 min*           <100ms            ~30s

* Sequential on Ollama, parallel on KServe (max_concurrent_calls).
```

### Stage 1: Python chunking — `app.core.chunker.chunk_vms`

Pure deterministic partitioning. Input: VM list + strategy + mapping.
Output: ordered list of `Chunk` objects.

The chunker classifies its dimensions:

| Tier | Dimensions | Rule |
|------|------------|------|
| **Mandatory** | `source_vcenter_id`, `target_ocp_cluster_id`, `classification_level`, `target_namespace` | Hard partitions. VMs that differ here MUST land in different chunks. |
| **Strong** | `application_hint`, `environment` | Preferred sub-partitions. Same chunk when sizes work; separate chunks when the result is more plannable. |
| **Soft** | `owner`, `os_family`, `primary_network` | LLM hints only. Chunker doesn't partition on these but passes them through as `chunk.hints` for the per-chunk prompt. |

When a chunk exceeds the active backend's
`max_planning_chunk_size`, the chunker subdivides:

1. **Tier detection** from name patterns (`web` / `app` / `db` /
   `cache` / `queue` / `lb` matched via regex against VM name and
   role).
2. **Primary network** when no tier signal exists.
3. **Even-sized splits** as last resort.

When a chunk falls below `MIN_CHUNK_SIZE` (5), the chunker tries to
combine it with siblings *that share the same partition_key*. This
keeps federal classification + namespace boundaries intact while
collapsing trivially small applications. **Foundation chunks merge
only with foundations** so the planner's foundation-first ordering
stays load-bearing. **Tiered chunks never merge** because their
dependency semantics matter more than chunk-size optimization.

The chunker also infers cross-chunk dependencies:

  - Foundation chunks → predecessors of every non-foundation chunk.
  - DB tier → predecessor of app tier → predecessor of web tier
    within the same application.
  - Production chunks depend on non-prod siblings of the same
    application.

These edges flow through to the assembled plan as wave-ordering
hints; the LLM's review pass sees them as `sequencing_decisions` in
its summary input.

### Stage 2: Per-chunk LLM planning — `app.core.chunked_planner._generate_plan_for_chunk`

One LLM call per chunk. The prompt is narrower than the v0.1.x
single-shot prompt because the chunker has already made the
partitioning decisions: the LLM only has to sequence the chunk's VMs
into waves.

Input: chunk metadata + chunk's VM list + customer strategy fragments.
Output: 1-3 waves with rationale + risk level + per-wave applications.

When the active backend declares
`supports_concurrent_calls=True` and `max_concurrent_calls > 1`, the
orchestrator runs chunks in parallel via `asyncio.gather` bounded by
a semaphore. KServe with 3 replicas sees ~3× speed-up; Ollama
serializes regardless of how many calls we issue.

### Stage 3: Python assembly — `app.core.chunked_planner._assemble_master_plan`

Walks chunks in dependency-friendly order (the chunker's
`_ordering_key`: foundations → partition → tier) and renumbers waves
sequentially across the union. Each wave row carries its `chunk_id`
so the UI can group waves under their owning chunk.

After assembly, `_validate_assembled_plan` re-checks integrity: every
input VM appears exactly once, no duplicates, wave numbers
sequential. This is belt-and-suspenders — the chunker validated
input partitioning and the per-chunk parser validated each chunk's
output — but federal customers take wave assignments at face value,
so a final cross-check is worth the cycles.

### Stage 4: LLM review — `app.core.chunked_planner._run_review`

A single chunk-level summary feeds this call. The LLM sees:

  - per-chunk metadata (label, VM count, applications, wave count,
    risk level),
  - the chunker's sequencing decisions,
  - the customer's strategy choices and freeform constraints.

It does **not** see individual VMs. Input stays under ~2K tokens
regardless of total fleet size — that's the architecture's scaling
property.

Output: executive summary, cross-chunk warnings, sequencing
rationale, customer review points. These land on the
`MigrationPlan.plan_summary` / `rationale` / `warnings` /
`next_actions` columns and surface in the plan view's headline
sections.

If the review LLM call fails, the orchestrator degrades gracefully
to a default executive summary and surfaces the failure as a
warning rather than failing the whole plan.

---

## Backend-aware capacity

Each `LLMBackend` declares:

```python
max_planning_chunk_size: int   # VMs per chunk LLM call
max_context_tokens: int         # model's context window
supports_concurrent_calls: bool
max_concurrent_calls: int
```

| Backend | chunk size | context | concurrent |
|---------|-----------:|--------:|:----------:|
| Ollama (Llama 3 8B) | 15 | 8192 | ❌ |
| KServe (RHOAI, Llama 3.1) | 50 | 32K | ✅ × 3 |
| vLLM (large context) | 75 | 128K | ✅ × 5 |

The chunker reads `max_planning_chunk_size` from the active backend.
Bumping the limits as new models or serving modes land is a
one-attribute change; orchestrator and chunker code don't need to
move.

---

## Single-shot fallback

`SINGLE_SHOT_THRESHOLD = 10` VMs. Below it, the orchestrator skips
chunking and routes through the legacy `StrategyPlanner` —
hierarchical overhead (chunker + assembly + review LLM call) isn't
worth the cycles for a 5-VM lab plan.

The plan row records which path was taken in the audit log
(`plan.generated.details.path_taken = "single_shot" | "hierarchical"`)
so federal reviewers can correlate behavior over time.

---

## Persisted chunk model

`PlanChunk` rows live on the `plan_chunks` table (cascade-delete
from `migration_plans`). Each row carries:

  - the chunker's `partition_key` and `sub_key` so reviewers see
    *why* the boundary was drawn,
  - the chunker's `hints` (soft signals the LLM was given),
  - the per-chunk LLM's `chunk_rationale` and `chunk_risk_level`,
  - the assembled-plan wave numbers that ended up inside this chunk.

The UI's `/plans/{id}` page reads these via
`GET /api/plans/{id}/chunks` and renders an expandable chunk
navigation alongside the wave list.

---

## When the architecture would break down

Three categories of pathological inventory the chunker won't help with:

### 1. One enormous monolith

A single application with 500 VMs and no tier signal. The chunker
falls through to even-sized splits, but the resulting chunks have
no semantic meaning — they're just slices of a list. The per-chunk
LLM has to make wave decisions without knowing what the application
does, so rationales degrade.

**Recovery:** populate the `application_hint`, `role`, and naming
patterns. The chunker's tier detection works on names; even a partial
naming convention helps a lot.

### 2. Cross-application dependencies the chunker can't see

Application A in chunk 3 talks to application B in chunk 7 via a
custom protocol the chunker doesn't model. The per-chunk LLM doesn't
see chunk 7 when planning chunk 3, so dependency stays implicit.

**Recovery:** the customer's freeform constraints field rides through
into every per-chunk prompt verbatim. Operators document
cross-application coupling there ("App-A authentication depends on
App-B's session store; migrate App-B first") and the LLM honors
it.

### 3. Strategy choices that conflict with chunker partitions

The strategy says "all dev before any prod" (sequential environments)
but the chunker has interleaved chunks. The Python ordering pass
prioritizes the chunker's structural rules (foundation → tier →
classification) over the strategy's wave-ordering preference.

**Recovery:** for these, the operator runs the planner once, reviews
the warnings the review LLM call surfaces, and either edits the plan
manually (move-vm endpoint) or re-runs with a tighter scope filter.

---

## Reading the code

```
backend/app/core/
├── chunker.py                  Stage 1 — pure-Python partitioning
├── chunked_planner.py          Stages 2-4 orchestrator
├── strategy_planner.py         Legacy single-shot path (fallback)
└── plan_generation.py          BackgroundTask + DB persistence

backend/app/models/
├── chunk.py                    PlanChunk row
├── plan.py                     MigrationPlan + PlanningStrategy

backend/tests/
├── test_chunker.py             Chunker unit tests (21)
├── test_chunked_planner.py     End-to-end with mocked LLM (9)
└── test_strategy_planner.py    Legacy single-shot tests (30)
```

---

## Related docs

- `docs/PLANNING_GUIDE.md` — operator-facing wizard walkthrough.
- `docs/LLM_TUNING.md` — context windows, batch sizes, retry behavior.
- `docs/SCALE.md` — tested capacity and per-backend recommendations.
- `docs/LLM_PROMPTS.md` — system prompts for each LLM call.
