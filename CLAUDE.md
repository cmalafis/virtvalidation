# VirtValidate — AI VM Migration Validation Platform

## LLM Input Discipline (Architectural Rule)

**Maximum per-LLM-call input: 10 items.** This is PER-CALL, not
per-feature. Features that need to reason over more must DECOMPOSE
into multiple LLM calls — each at ≤10 items — and orchestrate them
mechanically.

When designing features that involve LLM calls, the LLM must NEVER
receive raw collections of items larger than `LLM_MAX_ITEMS_PER_CALL`
(default: 10; see `app.core.config.settings.llm_max_items_per_call`).
Always pre-process with deterministic Python rules to reduce the
LLM's input to a small number of pre-formed groups or decisions.

The LLM is for genuinely ambiguous judgment calls, not for
categorization or grouping that could be done mechanically.

### Why

- LLMs drop tokens in long structured outputs. Proven empirically in
  May 2026 testing: Llama 3.2 3B failed at 20 VMs, Granite 3.1 8B
  failed at 57. Symptoms are silent — dropped vm_ids,
  duplicated entries across waves, schema violations.
- Deterministic Python is faster, auditable, and bug-free.
- Federal customers require auditable decisions; LLMs are
  harder to audit than Python rules.
- Most "AI" features have a mechanical core; the LLM is just
  for the genuinely ambiguous edges.
- This scales without requiring larger models.

The 20-item ceiling is empirical and may be adjusted upward as
models improve, but only with explicit benchmarking against the
production model. Default to assuming the ceiling holds.

### Design-review checklist

When proposing a feature that involves an LLM, the design must
explicitly answer:

1. What is the maximum size of the LLM's input collection?
2. If > 20 items, what mechanical pre-processing reduces it?
3. What would happen if the LLM dropped or duplicated outputs?
4. Is there a Python-only fallback path?

No LLM feature ships without addressing all four.

### Output validation + retry + fallback (architectural rule)

Every LLM feature MUST:

  - **Validate** the LLM's output against the schema/invariants the
    caller depends on. Don't trust the model.
  - **Retry on validation failure** with corrective feedback — the
    previous attempt's error becomes the first line of the next
    prompt. Default 3 attempts. Smaller models self-correct more
    reliably with specific error feedback than with general
    instructions.
  - **Fall back to mechanical/deterministic logic** when every
    retry fails. The user must always receive a valid result —
    never a 502 because the LLM had a bad day. Plans produced by
    the fallback are LESS NUANCED than LLM plans, not less valid.

The mechanical fallback is **not a failure mode** — it's an
architectural choice. Federal customers require auditable
behavior; "the LLM returned garbage three times and we sent an
error" is not auditable. "The LLM returned garbage three times
and we ran the deterministic assigner" is.

Surface the path taken via a `method` field
(`"llm"` / `"llm_retry_N"` / `"mechanical_fallback"`) so operators
can debug LLM quality regressions from the audit log.

Reference: `app.core.planner.MigrationPlanner._assign_waves_with_retry` +
`_mechanical_assign_waves`.

### Decompose for unbounded collections

For features that process unbounded collections (plans, validations,
baselines):

  - Decompose into per-item or per-group LLM calls.
  - Parallelize where possible.
  - **Never assemble a "global view" for a single LLM call.**
  - Mechanical orchestration in Python; LLM for local judgment.

Anti-patterns:

  - **Bad**: "LLM, here's all 1000 baselines. Compare them."
    **Good**: "LLM, here's one baseline diff (15 lines). Explain it."
  - **Bad**: "LLM, here's the whole plan. Critique it."
    **Good**: "LLM, here's wave 3 (8 groups). Critique just this wave."
  - **Bad**: "LLM, here are 50 categorized groups. Order them into waves."
    **Good**: Python orders the groups deterministically; LLM writes
    rationale for one wave at a time.

### Reference implementations

  - `backend/app/core/preclassifier.py` — N VMs → unbounded
    mechanical groups by vCenter / namespace / network / datastore /
    application_hint / role. Pure Python, deterministic.
  - `backend/app/core/wave_skeleton.py` — groups → waves with hard
    limits (≤10 VMs per wave, ≤2 HA peers per family per wave).
    Pure Python, deterministic. The LLM does not influence wave
    structure.
  - `backend/app/core/planner.py::_fill_wave_rationale` — per-wave
    rationale, one LLM call per wave. Each call sees ≤10 groups
    (because wave count is capped), so the per-call ceiling holds
    regardless of overall plan size.

### Good vs bad shaping examples

| Bad                                            | Good                                                |
|------------------------------------------------|-----------------------------------------------------|
| "Group these 100 VMs into waves"               | "Assign these 8 pre-formed groups to waves"         |
| "Categorize these 500 log lines"               | "Categorize these 12 deduplicated log patterns"     |
| "Compare these 200 config diffs"               | "Explain these 5 unique diff categories"            |

### Legitimate LLM use (small input, ambiguous decision)

- Wave ordering decision for 5-15 pre-formed groups
- Risk classification for one VM with ambiguous attributes
- Rationale generation for one wave's contents
- Migration recommendation for one unclear validation result

Cite this rule by name in any PR or design discussion that proposes
a new LLM-driven feature.

---

## What this is
Local appliance that validates VMs migrated from VMware to OpenShift
Virtualization using SSH + local LLM reasoning. Air-gapped by design.

## Stack
- Frontend: React 18 + Tailwind + Vite
- Backend: Python 3.12 + FastAPI
- LLM: Ollama + Llama 3 8B (local, never calls external APIs)
- DB: PostgreSQL 16
- Containers: Podman (rootless) + podman-compose for dev, Quadlet for prod
- SSH: Paramiko + Ed25519 keys

## Core modules
1. Pre-flight capture — SSH into VMware VMs, collect baseline state
2. Validation engine — SSH into OCP-Virt VMs, diff vs baseline, LLM reasons
3. Migration planner — LLM groups VMs into dependency-ordered waves

## Hard rules
- NEVER call external APIs or LLM services — air-gapped by design
- All LLM calls go to Ollama at $OLLAMA_HOST (default: http://ollama:11434)
  except in dev where `LLM_BACKEND_TYPE=mock` swaps in an in-process
  canned-response backend (see `docs/MOCK_BACKEND.md`). When adding a
  new LLM-consuming flow, also extend `MockBackend._INTENT_KEYWORDS`
  + a `_<intent>_response()` method so dev/CI runs without a real LLM.
  The mock MUST emit the same JSON shape the new flow's parser
  accepts; pin it with a round-trip test in
  `tests/test_mock_backend.py`.
- SSH uses Ed25519 keys only — stored in /app/keys/, never baked into images
- Use Podman — NOT Docker. Containerfiles NOT Dockerfiles.
- Volume mounts use :Z SELinux label for RHEL/Fedora compatibility
- Images always reference docker.io/ or registry.access.redhat.com/ explicitly
- **Container base images MUST be Red Hat UBI 9.** Federal customer
  security reviews reject Docker Hub bases, and FIPS validation
  requires the in-container OpenSSL to come from the same supply
  chain as the FIPS-enabled host kernel. Don't swap to community
  images even temporarily — the FIPS posture quietly breaks. The
  three blessed bases are:
  - `registry.access.redhat.com/ubi9/python-312:latest` (backend)
  - `registry.access.redhat.com/ubi9/nodejs-20:latest` (frontend builder)
  - `registry.access.redhat.com/ubi9/nginx-124:latest` (frontend runtime)
  See `docs/CONTAINER_IMAGES.md` for the full rationale, image
  layout, scanning procedure, and air-gapped mirroring guidance.

## Architecture documentation
- `docs/ARCHITECTURE.md` and `docs/architecture-diagram.html` are
  **generated** from source by `scripts/generate_architecture_docs.py`.
- When you add a new function, class, API endpoint, SQLAlchemy model,
  Pydantic schema, or React component, regenerate before committing:
  `python3 scripts/generate_architecture_docs.py`
- The drift-check CI workflow was removed in the CI simplification
  (May 2026). The script is still the source of truth; CI no longer
  enforces freshness. If you forget to regenerate, the docs go stale
  silently — review your diff before pushing.
- `docs/product-map.html` is hand-maintained — describes *what* the
  product does. The architecture diagram describes *how* it's built.

## Schema change workflow (NON-NEGOTIABLE)

The app applies Alembic migrations at startup via
`app.core.migrations.apply_migrations`. There is **no fallback to
`Base.metadata.create_all`** — schema changes that lack a migration
will crash the app on next deployment.

When you add or modify a SQLAlchemy model in `backend/app/models/`,
you MUST also create an Alembic migration in the same change:

1. Make the model change.
2. Generate the migration:
   ```bash
   cd backend
   ALEMBIC_DATABASE_URL=sqlite:////tmp/migration_gen.db \
       alembic revision --autogenerate -m "describe the change"
   ```
3. **Always review the generated file before committing.**
   - Check it matches your intent.
   - Edit if autogenerate produced wrong output (it sometimes
     misses enum changes, JSON columns, or index renames).
   - Add data migrations if needed (autogenerate only does schema).
4. Test the upgrade path against a fresh DB:
   ```bash
   ALEMBIC_DATABASE_URL=sqlite:////tmp/migration_test.db \
       alembic upgrade head
   ```
5. Test the rollback path:
   ```bash
   ALEMBIC_DATABASE_URL=sqlite:////tmp/migration_test.db \
       alembic downgrade -1
   ALEMBIC_DATABASE_URL=sqlite:////tmp/migration_test.db \
       alembic upgrade head
   ```
6. Commit BOTH the model change AND the migration file in the
   same commit. CI runs `tests/test_migrations.py::test_models_match_migrations_no_drift`
   which fails the build if a model field exists without a
   corresponding migration.

See `docs/DATABASE_MIGRATIONS.md` for the full workflow including
data migrations, rollback recovery, and the legacy-`create_all`
bridge for v0.1.x deployments.

## Enum I/O rules (names vs. values on both sides)

Whenever a Python `str` enum has member NAMES that differ from member
VALUES (e.g. `rwx = "ReadWriteMany"`), the codebase needs explicit
serialization config on BOTH sides of every I/O boundary — otherwise
SQLAlchemy / Pydantic default behavior leaks the wrong string and
something at the other end rejects it.

**Output side (API → frontend):**

```python
class FooRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)
    state: FooState
```

Without `use_enum_values=True`, FastAPI emits the member NAME instead
of the VALUE, breaking any frontend filter that uses the value string.

**Input side (SQLAlchemy → Postgres):**

```python
state: Mapped[FooState] = mapped_column(
    Enum(FooState, name="foo_state",
         values_callable=lambda e: [m.value for m in e]),
    ...
)
```

Without `values_callable`, SQLAlchemy `INSERT`s the member NAME, and
Postgres rejects with `invalid input value for enum foo_state: "rwx"`
when the Alembic migration created the `CREATE TYPE` with the values
(`"ReadWriteMany"`). The `values_callable` callback must match what
the migration's `sa.Enum(...)` literal list contains.

**Skip both** when `member = "member"` style (names == values). The
default behavior is correct in that case and adding the kwargs is
noise. Most enums in this codebase fall here — `available =
"available"`, `discovered = "discovered"` etc.

**Audit shortcut**: grep `app/models/*.py` for `Enum(` to find every
column. For each, check the surrounding enum class definition: if
the right-hand-side of any member is not a copy of the left-hand-side
identifier, the column needs `values_callable`.

Known wart: `ValidationStatus` (`passed = "pass"`, `failed = "fail"`)
has names ≠ values BUT its migration mistakenly used the NAMES on
`CREATE TYPE`, so SQLAlchemy's default (send NAME) accidentally
matches Postgres's type. It works in production but is semantically
wrong. Don't fix it without a coordinated migration + frontend
audit — the API output today is the NAME, not the VALUE.

## Frontend orphans (rewrite-without-rewire)

When a session rewrites a frontend component (e.g. PlanWizard,
GeneratePlanModal) it MUST either delete the old component or rewire
its call site. A rewritten file alone isn't enough — Vite happily
bundles unreferenced components and the dashboard keeps importing
the old one. Two failure modes:

1. **New component is route-mounted only, dashboard still opens old
   modal.** The new code is in the bundle but unreachable. Operator
   sees no change.
2. **Two components with the same title coexist.** The wrong one
   wins because nothing wires the new one in.

Before declaring "I rewrote X":

1. `grep -rn 'OldComponentName\|<distinctive title string>' frontend/src/`
   to find every reference. If anything outside the rewritten file
   matches, decide: delete it, or rewire it.
2. Verify by clicking through the deployed UI, not by checking that
   the bundle's hash changed or that a new string is present in the
   minified output. Bundle changes don't prove the new code is on
   the operator's screen.

## Deployment template edits (Helm + Containerfiles)

These traps were caught on the first real OpenShift deployment.
The full live-debug log lives in `docs/DEPLOYMENT_TROUBLESHOOTING.md`.

- **Env var ordering matters.** Kubernetes' `$(VAR)` substitution
  only sees env vars defined **earlier** in the same container's
  `env:` list. `DATABASE_URL` MUST come AFTER `POSTGRES_USER` /
  `POSTGRES_PASSWORD`, or kubelet leaves the placeholders as
  literal strings and Postgres rejects auth.
- **nginx.conf is now a template.** Don't hard-code service names.
  Use `${BACKEND_HOST}` / `${BACKEND_PORT}` placeholders;
  `frontend/entrypoint.sh` renders them via `envsubst` at start.
  The Helm chart's deployment-frontend.yaml provides
  release-prefixed values; podman-compose.yml uses the literal
  `backend`.
- **UBI nginx tmp dirs.** The Helm chart's emptyDir mounts on
  `/tmp`, `/var/lib/nginx`, and `/run` overlay baked-in
  subdirectories. `frontend/entrypoint.sh` mkdir's `/tmp/nginx/*`
  at runtime after the mount — don't rely on RUN mkdir in the
  Containerfile for these paths.
- **Cross-arch builds.** `scripts/build-images.sh` auto-detects
  host=arm64 + target=linux/amd64 and falls back to
  `frontend/Containerfile.runtime` after a native `npm run build`,
  bypassing the QEMU esbuild crash. Override with `PLATFORM=`.
- **Postgres image swap.** The Red Hat
  `registry.redhat.io/rhel9/postgresql-16` uses `POSTGRESQL_*`
  (with the QL) env vars and writes to `/var/lib/pgsql/data`.
  Toggle via `postgres.image.useRedHatImage: true` — the chart
  renders matching env aliases + mountPath. See
  `docs/CONTAINER_IMAGES.md` for the pull-secret recipe.
- **Bump `Chart.yaml.version` whenever a template changes.** Helm
  upgrade can miss order-only changes on existing deployments
  without a version bump; the version delta forces a rollout.

## Plans partition by (vcenter, target_namespace, environment)

The preclassifier's primary partition key is the triplet
(source_vcenter_id, target_namespace, environment). Production
VMs never share a plan with development or DR VMs — even when
both land in the same target namespace — because operators
sequence those lifecycle stages separately in practice.

`VM.environment` accepts free-text values for back-compat; the
preclassifier normalizes through
`app.core.environment.normalize()` so `"Prod"` / `"production"` /
`"PRODUCTION"` / `"prd"` all collapse to the same enum value
before partitioning.

When adding new partition dimensions (e.g. target_cluster_id when
the OCP cluster registration refactor lands), extend the
`primary_key` tuple in `app.core.preclassifier.PreClassifier.classify`
**AND** add the field to `GroupKey` so audit log group ids stay
unique across partitions. The `_build_group` callers also need
to be threaded with the new field.

Auto-detection of `environment` from VM name / folder / cluster
/ custom_attributes is in `app.core.environment.detect_environment`
— wire it into any new VM creation path. See
`docs/ENVIRONMENT_LABELS.md`.

## Migration plan pipeline (Stages 0-7, deterministic + LLM annotation)

`app.core.plan_pipeline.run_pipeline` is the single entry point for
plan generation. The stages:

  0. **Validate mapping coverage** (`mapping_validation.py`) —
     every selected VM must resolve to a target NAD + StorageClass
     + namespace. Returns 422 with VM-level gap detail.
  1. **Hard partition** (`preclassifier.classify`) — by
     `(source_vcenter_id, target_namespace, environment_normalized)`.
  2. **Sub-partition** — within each primary, by network /
     datastore / role / application_hint overlap.
  3. **HA family anti-affinity split** (`family.split_overconcentrated_families`)
     — any group with more than ceil(family_size/2) members of one
     family is split into sub-groups. Family detection in
     `family.detect_family` is name-based; the existing HA-aware
     wave-skeleton logic refines on top.
  4. **Wave packing** (`wave_skeleton.MechanicalWaveAssigner`) —
     greedy, deterministic, ≤10 VMs/wave, ≤2 HA peers/wave,
     partition coherence (one MTV Plan CR per wave).
  5. **Concurrency analysis** (`concurrency.assign_concurrency_groups`)
     — graph-color waves so two waves with the same
     `concurrency_group_id` are parallel-safe (different vCenters
     AND disjoint families).
  6. **Per-wave LLM annotation** (`wave_annotation.annotate_waves`)
     — one LLM call per wave, ≤10 groups per call, parallel via
     `asyncio.gather` under `Semaphore(backend.max_concurrent_calls)`.
     Validate-retry-fallback per the architectural rule above.
     Each wave's `method` is `"llm"` / `"llm_retry_N"` /
     `"mechanical_fallback"`.
  7. **MTV YAML emission** (`plan_pipeline.emit_wave_yaml` wraps
     `mtv.generate_wave_yaml`) — per-wave NetworkMap + StorageMap
     + Plan CR. No placeholder names; Stage 0 catches gaps before
     emission.

Stages 0-5 + 7 are pure Python and run in well under 1s for 250 VMs.
Stage 6 wall-clock is dominated by the slowest single LLM call,
not their sum, because of `asyncio.gather`.

Selection cap: `settings.max_vms_per_plan` (default 250). `POST
/api/plans` returns 422 above the cap so the operator narrows
filters or splits into multiple plans rather than running one
huge black-box plan.

When adding a new pipeline stage, extend
`plan_pipeline._PIPELINE_STAGE_TO_STATUS` so the Plan row's
`status` field reflects the new step name; the frontend's poll
loop picks up new stages without further changes.

## VM lifecycle (plan membership, parallel to VM.status)

`VM.lifecycle_state` answers "is this VM available for a new
plan?". It's server-enforced through `app.core.vm_lifecycle`:

```
available  → planned          POST /api/plans
planned    → migrated         POST /api/plans/{id}/mark-succeeded
planned    → available        DELETE /api/plans/{id}  OR  plan→failed
migrated   → rolled_back      PATCH /api/vms/{id}
rolled_back → available       PATCH /api/vms/{id}
```

`VM.status` (discovered → baseline_captured → migrated → validated
→ failed) is orthogonal — it tracks the baseline/validation
lifecycle, NOT plan membership. The "migrated" overlap is a
naming collision: `status=migrated` means "we observed the VM
running on OCP-Virt"; `lifecycle_state=migrated` means "operator
declared this plan succeeded". Both can be set independently.

The plan selector hides anything not `available` by default;
inventory shows the lifecycle pill on every row and surfaces
"Revert to VMware" + "Make available" affordances on the
operator-driven transitions.

## Paginated list endpoint pattern

Listing endpoints with > a few hundred rows MUST return a wrapped
response — `{items: [...], total: N, skip: N, limit: N}` — not a
raw list. `GET /api/vms` is the reference. Routes that return raw
lists silently truncate at the page size and force the frontend
to load everything just to render a counter.

When adding a new paginated listing:

1. Define a `*ListResponse` schema in `app/schemas/<resource>.py`
   with `items / total / skip / limit`.
2. Accept `skip`, `limit`, `sort_by`, `sort_order`, and any
   filter dimensions as `Query(...)` params.
3. For multi-value filters, type the param as
   `list[Type] | None = Query(default=None)` — FastAPI handles
   `?status=a&status=b` natively.
4. Add a `/facets` endpoint if the frontend filter UI shows
   counts per value.
5. Add a `/stats` endpoint if the dashboard header reads counts
   from the list — don't reuse the paginated query.
6. Add a `DELETE /<resource>/all` endpoint with `?confirm=true`
   if operators need a wipe action. Re-use the same `_apply_filters`
   builder so the filter semantics match the listing.

See `docs/INVENTORY_GUIDE.md` for the inventory-table contract
and `backend/tests/test_inventory_pagination.py` for the pins.

## Repo structure
virtvalidate/
├── frontend/          # React app
├── backend/           # FastAPI
│   └── app/
│       ├── api/       # Route handlers
│       ├── core/      # SSH engine, LLM client
│       ├── models/    # SQLAlchemy models
│       └── main.py
├── infra/
│   └── quadlet/       # Systemd Quadlet units for prod deployment
├── podman-compose.yml # Dev: one command to run everything
├── CLAUDE.md          # This file — read at start of every session
└── .env.example       # Config template

## Current status
- [x] Frontend dashboard (React) — VirtValidate.jsx
- [x] Backend API skeleton
- [x] SSH collection engine
- [x] LLM validation engine
- [x] Migration planner (Stages 0-7; see "Migration plan pipeline" above)
- [x] PostgreSQL models
- [x] VM plan-membership lifecycle (`VMLifecycleState`)
- [x] PlanWizard per-VM selector with selection cap + filters
- [x] Inventory revert affordances (migrated → rolled_back → available)

## DEFENSIVE CODING REQUIREMENTS

When writing React components that consume API data, ALWAYS:

1. Assume any nested field might be undefined or null
2. Use optional chaining (?.) and nullish coalescing (??) for all 
   nested property access
3. Provide empty array defaults: const items = data?.items ?? []
4. Render explicit empty states for missing data, never let the 
   component crash
5. Wrap pages in error boundaries
6. Test components with: no data, partial data, error responses, 
   loading states

Common patterns that crash:
- array.length when array is undefined → use (array || []).length
- object.field when object is undefined → use object?.field
- array.map(...) when array is undefined → use (array || []).map(...)
- nested.path.access → use nested?.path?.access

When writing API endpoints, ALWAYS return consistent response 
shapes. Don't return 404 for "no data yet" — return 200 with 
empty arrays/null fields. The frontend should never have to 
distinguish between "endpoint doesn't exist" and "endpoint exists 
but no data."
