# Environment Labels

VirtValidate labels every VM with its lifecycle environment —
production, development, DR, etc. — so the planner can partition
mixed-environment fleets into separate migration plans. Production
VMs never share a plan with development or DR VMs, even when both
land in the same target namespace, because operators cut over
those tiers separately in practice.

This document covers the typed enum, the auto-detection cascade,
and the operator workflow.

---

## Why environments matter for migration

Federal customer migrations are sequenced by lifecycle stage:

  - **Production** moves in maintenance windows, with rollback plans
    and downtime budgets.
  - **Development** moves opportunistically and accepts higher
    cutover risk.
  - **DR** moves on its own cadence — typically AFTER production so
    the DR cluster mirrors the new prod posture.
  - **Infrastructure** (AD, DNS, PKI) moves FIRST so dependent
    services have a quorum during their cutovers.

Mixing these in one wave is the source of every "we lost prod
during the dev migration" incident the field reports. The planner
treats environment as a primary partition dimension to make that
class of mistake structurally impossible.

---

## The Environment enum

`app.core.environment.Environment`:

| Value             | When |
|-------------------|------|
| `production`      | Live customer-facing workload. |
| `development`     | Engineering / sandbox. |
| `test`            | QA / UAT / acceptance environments. |
| `staging`         | Pre-prod release-candidate validation. |
| `dr`              | Disaster recovery / replicated copy. |
| `infrastructure`  | Shared services (AD/DNS/PKI). |
| `db_only`         | Stateful data-tier VMs without other env signal. |
| `non_prod`        | Catch-all for non-production environments. |
| `unknown`         | No signal matched. Operator must label. |

The string values are stable — the audit trail uses them verbatim,
so changing one is a breaking change that requires a migration.

---

## The auto-detection cascade

`app.core.environment.detect_environment` runs a 5-tier cascade.
First match wins. Each tier carries a confidence bucket
(`high` / `medium` / `low`) that operators use to triage which
auto-detected labels to review.

| Tier | Signal                                | Confidence |
|------|---------------------------------------|------------|
| 1    | Explicit operator label (override)    | high       |
| 2    | RVTools `custom_attributes["Environment"]` | high   |
| 3    | Folder path (`/prod`, `/dev`, …)      | high       |
| 4    | Cluster name pattern (`prod-east`)    | medium     |
| 5    | VM name prefix (`prod-`, `dev-`)      | low        |
| 6    | DB heuristic (`postgres`, `mongo`, …) | low        |
| —    | None matched                          | low / `unknown` |

`UNKNOWN` is the safe default. Operators see UNKNOWN VMs flagged
in the inventory UI and bulk-label them before generating a plan.
The planner refuses to silently mix UNKNOWN with labeled VMs —
UNKNOWN partitions on its own so the operator notices.

### Normalization

`normalize(value)` accepts the wide spectrum of free-text labels
federal customers ship in RVTools custom-attributes columns:
`prod`, `Prod`, `PRODUCTION`, `prd`, `live` → all map to
`Environment.PRODUCTION`. Prefix matching catches
`prod-east-01` and `disaster-recovery-cluster` too.

See `backend/tests/test_environment.py` for the full alias list.

---

## Plan partitioning by environment

The preclassifier's primary partition key was extended from
`(vcenter_id, target_namespace)` to
`(vcenter_id, target_namespace, environment)`. Groups never mix
environments — even when sharing vcenter + target namespace.

End-to-end: a fleet with 30 prod VMs and 30 dev VMs in the same
target namespace produces a plan where every wave's groups are
single-environment. The wave packer may still co-locate distinct
prod-only and dev-only groups in the same wave if they fit the
per-wave VM cap, but no individual group ever mixes.

Pinned by `backend/tests/test_environment_partition.py`.

---

## Deferred work

This first cut delivers the typed enum + detection + partition.
Several spec items are explicitly **not** included in this
refactor and remain open follow-ons:

### Model migration to typed enum

`VM.environment` is still a free-text `String(64)` column. The
typed enum lives in `app.core.environment` and is applied via
`normalize()` at read time. A future Alembic migration will:

  - Add an `environment_source` column
    (`"unset"|"auto_detected"|"user_set"|"imported"`).
  - Convert `environment` to a constrained enum or check
    constraint over the canonical values.
  - Backfill existing rows by running `detect_environment` over
    each VM's name + (future) folder + cluster fields.

### RVTools import integration

`detect_environment` is not yet wired into the bulk-create path
(`POST /api/vms/bulk`). When the migration above lands, the
importer will:

  1. Run detection on every incoming VM.
  2. Set `environment` + `environment_source="auto_detected"`.
  3. Surface UNKNOWN VMs in the import-summary UI.

### VM model additions

The detection cascade reads `folder_path`, `cluster`, and
`custom_attributes` — but the VM model doesn't have those columns
today. Tier 2-4 of the cascade are only fully effective once
those columns land. Tracked in the spec's Part 1.2.

### Bulk relabel + redetect endpoints

`PATCH /api/vms/{id}/environment`, `POST /api/vms/bulk-set-environment`,
`POST /api/vms/redetect-environment` are open follow-ons. The
classifier function exists; only the API surface remains.

### Removing OCP cluster authentication

The spec calls for removing `api_token`, `ca_cert`, `verify_ssl`
from the `OCPTarget` model and dropping the discovery endpoints.
That's a separate large refactor (touches `app/models/target.py`,
`app/api/targets.py`, `app/core/ocp_discovery.py`, plus the
frontend OCPTargets page) and is deferred.

### Mapping templates

The spec's Part 4 — `NetworkMapping` and `StorageMapping` models
with per-cluster mappings — and Part 5 — per-VM
`target_cluster_id` override + `EnvironmentClusterMapping` — both
require new models, migrations, CRUD APIs, and frontend pages.
Open follow-ons.

---

## API surface (current)

  - `app.core.environment.Environment` — enum.
  - `app.core.environment.normalize(value)` — string → enum.
  - `app.core.environment.detect_environment(**signals)` →
    `DetectionResult(environment, confidence, signal)`.

The detection function is callable today from any backend code
that needs to classify a VM. The RVTools importer integration
that calls it at scale is the next step.

---

## Related docs

- [`docs/PLANNING_ARCHITECTURE.md`](./PLANNING_ARCHITECTURE.md) —
  the preclassifier partition flow that consumes environment.
- [`docs/TEST_DATA.md`](./TEST_DATA.md) — the DHA federal fleet
  fixture already populates `environment` on every VM.
- `backend/tests/test_environment.py` — pinned alias + cascade
  contracts.
- `backend/tests/test_environment_partition.py` — pinned partition
  invariants.
