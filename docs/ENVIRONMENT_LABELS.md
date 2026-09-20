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

## What's wired (P refactor)

The P refactor completed the wiring O deferred:

  - **VM columns** — `vsphere_cluster`, `vsphere_folder`,
    `custom_attributes` (JSON), `environment_source` columns
    landed in Alembic migration `20260512_3369ee37afd9`. Existing
    rows with non-null `environment` got `environment_source =
    "auto_detected"` in the backfill.
  - **RVTools parser** — `frontend/src/utils/parseRVTools.js` now
    extracts Cluster / Folder columns and the well-known custom-
    attribute columns (Environment, App, Tier, Owner,
    Application).
  - **Importer integration** — `POST /api/vms/bulk` runs
    `detect_environment` on every incoming VM that doesn't carry
    an explicit `environment` value. Operator-supplied values
    are tagged `user_set`; detector-supplied values are tagged
    `auto_detected`.
  - **Override endpoints** —
    `PATCH /api/vms/{id}/environment`,
    `POST /api/vms/bulk-set-environment` (atomic), and
    `POST /api/vms/redetect-environment` (with `force` / `dry_run`)
    are all live. Each writes an audit-log entry under the
    `vm.environment.*` actions.

## Deferred work

The following items from the spec remain open follow-ons. The
P refactor was scoped to the detection wiring; multi-cluster
routing and the larger UI surface are still ahead.

### Removing OCP cluster authentication

The spec calls for removing `api_token`, `ca_cert`, `verify_ssl`
from the `OCPTarget` model and dropping the discovery endpoints.
Separate large refactor — touches `app/models/target.py`,
`app/api/targets.py`, `app/core/ocp_discovery.py`, plus the
frontend OCPTargets page.

### Mapping templates + multi-cluster routing

The spec's Part 4 — `NetworkMapping` and `StorageMapping` models
with per-cluster mappings — and Part 5 — per-VM
`target_cluster_id` override + `EnvironmentClusterMapping` —
both require new models, migrations, CRUD APIs, and frontend
pages.

### UI flows

The Environment Labels page, Target Clusters page, Environment
Routing page, Mapping Templates pages, and the wizard partition
preview are all open. Backend endpoints for the labels workflow
are now wired (PATCH / bulk / redetect); the frontend doesn't
surface them yet.

### Per-vCenter source-signal aggregation endpoint

The mapping page (`ResourceMappings.jsx`) used to fetch
`limit=10000` VMs to aggregate which networks / datastores
exist in a vCenter — that's now capped at the backend's 1000-
page-size limit. For vCenters with >1000 VMs, aggregating
server-side via a dedicated
`GET /api/sources/vcenters/{id}/source-signals` endpoint would
restore full fidelity. Open follow-on.

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
- [`docs/TEST_DATA.md`](./TEST_DATA.md) — the reference federal fleet
  fixture already populates `environment` on every VM.
- `backend/tests/test_environment.py` — pinned alias + cascade
  contracts.
- `backend/tests/test_environment_partition.py` — pinned partition
  invariants.
