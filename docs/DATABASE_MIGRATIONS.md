# Database Migrations

VirtValidate uses [Alembic](https://alembic.sqlalchemy.org/) for every
schema change. The application applies pending migrations at startup
and fails fast if they don't apply cleanly. There is **no fallback to
`Base.metadata.create_all`** — that path was removed because it
silently dropped column additions and caused production data loss.

This document covers the operator + developer workflow.

---

## Why we don't use `Base.metadata.create_all` anymore

`Base.metadata.create_all` only creates tables that don't exist. It
does **not** add columns to existing tables, alter constraints, or
update enum types. We hit this bug repeatedly during v0.1.x:

  1. Developer adds a column to a SQLAlchemy model.
  2. Tests pass — pytest uses a fresh in-memory SQLite per test.
  3. Local `podman-compose up -d` works — Postgres volume is fresh.
  4. CI passes — same.
  5. Someone with an older Postgres volume restarts the container.
  6. App boots, `create_all` sees the table already exists, skips
     the new column entirely.
  7. First request that references the new column crashes with a
     `column "..." does not exist` error.
  8. The fix was always `podman-compose down -v` — destroying data.

Alembic produces explicit migration scripts for every change. The
runtime uses them; the test suite verifies model-vs-migration parity
on every CI run.

---

## When to create a migration

**Any change to a SQLAlchemy model** in `backend/app/models/`. That
includes:

  - Adding / removing / renaming a column.
  - Changing a column's type, nullability, or default.
  - Adding / removing an index, constraint, or foreign key.
  - Adding / removing a table.
  - Adding / removing / renaming an enum value.

If you're not sure whether your change needs a migration, run the
drift check:

```bash
cd backend
podman exec virtvalidation_backend_1 \
  python -m pytest tests/test_migrations.py::test_models_match_migrations_no_drift
```

If it fails, you need a migration.

---

## Creating a migration

The autogenerate command compares the live model state against an
empty (or up-to-date) database. Use a temporary SQLite file rather
than the production Postgres so you don't need `DATABASE_URL` set
correctly:

```bash
cd backend
rm -f /tmp/migration_gen.db
ALEMBIC_DATABASE_URL=sqlite:////tmp/migration_gen.db \
    alembic upgrade head
ALEMBIC_DATABASE_URL=sqlite:////tmp/migration_gen.db \
    alembic revision --autogenerate -m "describe the change"
```

The generated file lands in `backend/migrations/versions/`. **Always
review it before committing.** Autogenerate gets most things right
but occasionally misses:

  - Enum value additions / removals (Postgres-specific).
  - JSON column type changes.
  - Index renames (autogenerate sees a drop + create, not a rename).
  - Server defaults that involve `func.now()` vs `text(...)`.

Edit the migration if needed. Common edits:

  - Tighten `server_default` expressions for portability.
  - Add a data migration after a schema change (e.g., backfill a
    new NOT NULL column with a sensible default before adding the
    constraint).
  - Add explicit indexes that autogenerate didn't pick up.

### Data migrations

Autogenerate produces schema migrations. When a column rename or
column-type change requires data movement, add the data step
yourself:

```python
def upgrade() -> None:
    op.add_column("vms", sa.Column("application_hint", sa.String(128), nullable=True))
    # Data migration — backfill from the legacy notes field where possible.
    op.execute(
        "UPDATE vms SET application_hint = "
        "  CASE WHEN notes ILIKE '%epic-emr%' THEN 'epic-emr' "
        "       WHEN notes ILIKE '%cerner%' THEN 'cerner' "
        "       ELSE NULL END"
    )
```

Always wrap data migrations in idempotent SQL — operators may need
to re-run on a partial failure.

---

## Applying migrations

The application applies migrations at startup via
`app.core.migrations.apply_migrations`. You almost never need to run
`alembic upgrade head` manually in production — the lifespan does it
for you.

For local development:

```bash
cd backend
ALEMBIC_DATABASE_URL=sqlite:////tmp/migration_test.db \
    alembic upgrade head
```

Or against the running Postgres:

```bash
podman exec virtvalidation_backend_1 alembic upgrade head
```

The latter is useful when you're hot-reloading the backend and want
to apply a new migration without a full restart.

---

## Rolling back

```bash
# Roll back one migration:
alembic downgrade -1

# Roll back to a specific revision:
alembic downgrade <revision_id>

# Roll back everything (drops every application table):
alembic downgrade base
```

The application **does not auto-rollback** at startup. If a
migration applies cleanly but breaks production behavior, the
operator manually rolls back via the commands above and pins the
container to a previous version while the underlying issue is
investigated.

---

## Inspecting current state

The schema health endpoint reports the database's migration posture
without requiring shell access:

```bash
curl -s http://localhost:8000/api/health/schema | jq
```

Returns:

```json
{
  "current_revision": "7c34de23960e",
  "head_revision": "7c34de23960e",
  "is_up_to_date": true,
  "pending_migrations": [],
  "legacy_create_all": false
}
```

`legacy_create_all=true` means the bridge logic (see below) is
about to fire on the next restart. `is_up_to_date=false` with a
non-empty `pending_migrations` list means the app is running
behind the migration scripts — usually a deployment-order bug.

The same data is embedded under `components.schema` in
`/api/health/full`, so a single liveness probe gives you DB +
schema + LLM + FIPS posture in one round-trip.

---

## Legacy `create_all` bridge (v0.1.x → migrations)

If you're upgrading from a v0.1.x deployment that bootstrapped its
schema via `Base.metadata.create_all`, your database has all the
application tables but no `alembic_version` row. The first time the
v0.2+ application starts, `apply_migrations` detects this case and
**stamps** the database at the current head before running any
upgrade. This is a safe one-time operation — no data is touched, no
schema changes are applied; we just record the assumption that the
existing schema matches what the latest migration produces.

You'll see this in the logs:

```
WARNING  Database has application tables but no alembic_version row —
         treating as a legacy v0.1.x create_all deployment and stamping
         at head=7c34de23960e. Future schema changes will go through Alembic.
```

After the bridge fires once, every subsequent restart is a normal
`alembic upgrade head` no-op until a new migration ships.

If the legacy schema **doesn't** match the current head (e.g., a
v0.1.x deployment that lost a column to drift earlier), the bridge
will still stamp head, but subsequent application code may fail at
runtime when it hits the missing column. To recover: write a
**catch-up migration** that reconciles the actual DB state with the
expected head, mark it as a one-shot migration, and ship it.

---

## Recovering from migration drift

If `test_models_match_migrations_no_drift` fails, the model layer
and the migration layer have diverged. Fix:

  1. Identify the offending change with the test output.
  2. Run autogenerate to capture the missing piece:
     ```bash
     ALEMBIC_DATABASE_URL=sqlite:////tmp/drift_check.db \
         alembic upgrade head
     ALEMBIC_DATABASE_URL=sqlite:////tmp/drift_check.db \
         alembic revision --autogenerate -m "fix drift"
     ```
  3. Review and edit the generated migration if needed.
  4. Commit the migration alongside the model change.

If the drift was introduced by a previous commit that's already on
`main`, the recovery migration is the canonical way forward — don't
edit existing migration files in version control.

---

## Recovering from a failed migration in production

The app fails fast on migration errors — `SystemExit(1)` triggers a
container restart loop, which surfaces the failure loudly. To
recover:

  1. Pin the deployment to the previous container image (Helm
     `--set image.tag=...` or Kustomize image override).
  2. Verify the rollback by hitting `/api/health/schema` and
     confirming `current_revision` matches what the rolled-back
     image expects.
  3. Investigate the failure with `podman logs` or
     `kubectl logs` — the migration error is logged before
     `SystemExit`.
  4. Fix the migration in code, ship a new image, redeploy.

If the migration partially applied (Postgres transactional DDL
should prevent this for most schema changes, but some CONCURRENTLY
operations bypass it):

  1. Roll back to the last known-good revision: `alembic downgrade <rev>`.
  2. Verify schema state matches the rollback target.
  3. Re-deploy.

---

## CI integration

The migration test suite runs on every CI build:

```
tests/test_migrations.py::test_upgrade_head_applies_cleanly_against_empty_db
tests/test_migrations.py::test_downgrade_to_base_drops_every_application_table
tests/test_migrations.py::test_upgrade_downgrade_upgrade_round_trips_cleanly
tests/test_migrations.py::test_models_match_migrations_no_drift
tests/test_migrations.py::test_apply_migrations_stamps_legacy_create_all_databases
tests/test_migrations.py::test_apply_migrations_is_noop_on_already_migrated_db
tests/test_migrations.py::test_schema_status_reports_pending_when_not_upgraded
tests/test_migrations.py::test_schema_status_endpoint_returns_payload
tests/test_migrations.py::test_every_migration_has_upgrade_and_downgrade
tests/test_migrations.py::test_baseline_migration_has_no_predecessor
tests/test_migrations.py::test_migration_error_raised_when_alembic_ini_missing
```

A model change without a corresponding migration fails
`test_models_match_migrations_no_drift` and blocks the PR.

---

## Quick reference

| Task | Command |
|------|---------|
| Generate migration from model changes | `alembic revision --autogenerate -m "..."` |
| Apply migrations to a DB | `alembic upgrade head` |
| Roll back one migration | `alembic downgrade -1` |
| See current revision | `alembic current` |
| See migration history | `alembic history --verbose` |
| Stamp existing DB at current head (skip apply) | `alembic stamp head` |
| Check live schema posture | `curl http://host:8000/api/health/schema` |
