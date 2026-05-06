# Planning Architecture — Hierarchical Chunking

VirtValidate's plan generator is **hierarchical**: Python partitions
the inventory deterministically before any LLM call, the LLM reasons
about each chunk independently at appropriate scale, then Python
assembles the output and calls the LLM one more time for a high-level
review.

This document explains why we built it that way, what each stage owns,
and where the architecture's edges are.

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
