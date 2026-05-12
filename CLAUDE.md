# VirtValidate — AI VM Migration Validation Platform

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
- CI (`.github/workflows/architecture-docs.yml`) regenerates on every
  PR and fails the build if the committed docs differ — drift is caught
  at review time, not in production.
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
- [ ] Backend API skeleton
- [ ] SSH collection engine
- [ ] LLM validation engine
- [ ] Migration planner
- [ ] PostgreSQL models

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
