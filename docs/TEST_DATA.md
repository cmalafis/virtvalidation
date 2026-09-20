# Test Data

VirtValidate's planner + classifier tests are pinned against a
synthetic hospital scenario — fictional but representative of the
kinds of environments the product targets. This document explains what
the test data represents and why it's structured the way it is.

If you're adding new planner / preclassifier tests and want a
non-trivial fleet to assert against, reach for the reference
fixture described below rather than rolling your own one-off VMs.

---

## Reference fleet — 57 VMs across 8 applications

### Where it lives

`backend/tests/fixtures/sample_fleet.py` builds the fleet
programmatically. Two entry points:

  - `build_sample_fleet_vms()` — detached `VM` model instances with
    assigned IDs. Use for unit tests that don't need a DB session.
  - `persist_sample_fleet(db_session)` — inserts the rows into the
    given session. Use for API / integration tests.

### Why a fixture and not an .xlsx

We considered shipping a real RVTools-format `.xlsx` test export.
Trade-offs:

  - **xlsx pros**: Visually inspectable in Excel; mirrors operator
    workflow; can be uploaded through the real RVTools import
    endpoint end-to-end.
  - **xlsx cons**: Binary file in git; opaque to code review;
    diff noise; needs `openpyxl` to read.

The fixture wins for `tests/` because every assertion the
preclassifier + planner tests make can be expressed against Python
objects directly. End-to-end RVTools upload tests still live
separately (`tests/test_rvtools_*.py`); they don't need this
specific fleet shape.

### The fictional scenario

The fleet represents a **fictional health-care provider** running
a mix of regulated + commercial applications:

| Application       | VMs | Role tiers                              | Environment     |
|-------------------|----:|-----------------------------------------|-----------------|
| EHRPro            | 12  | 3 web + 4 app + 3 data + 2 worker       | production      |
| PACSImaging       | 10  | 2 web + 4 app + 2 data + 2 imaging      | production      |
| IdentityServices  |  8  | 4 AD-DC + 2 RHIDM + 2 PKI               | production      |
| InfraServices     |  6  | 2 DNS + 2 NTP + 2 Backup                | production      |
| LegacyApp         |  8  | 2 web + 4 app + 2 data                  | production      |
| DevSan            |  6  | (dev sandbox)                           | development     |
| EHRStaging        |  4  | 1 web + 2 app + 1 data                  | development     |
| EHRDR             |  3  | web/app/data (DR copy)                  | dr              |
| **Total**         |**57** | spread across 3 vCenters              | prod + dev + DR |

### Metadata populated on every VM

Each `FleetVMSpec` sets:

  - `name` — e.g. `ehrpro-postgres-db-01`. Role-revealing prefix +
    application + sequence number, following a naming convention
    typical of large regulated fleets.
  - `application_hint` — `ehrpro`, `pacsimaging`, etc. The operator-
    supplied custom attribute the preclassifier uses as the PRIMARY
    cohesion signal.
  - `environment` — `production` / `development` / `dr`.
  - `source_vcenter_id` — 1 (prod), 2 (dev), 3 (DR).
  - `target_namespace` — `<app>-prod` / `<app>-staging` / `<app>-dr`.
  - `vsphere_networks` — VLAN-tagged per tier
    (`VLAN-100-Web-Prod`, `VLAN-110-App-Prod`, …).
  - `vsphere_datastores` — `prod-gold-ssd-01` (data tier),
    `prod-silver-hdd-01` (app tier), `prod-bronze-archive` (workers,
    backup), `infra-shared-01`, `dev-shared-01`, `dr-shared-01`.
  - `role` — the operator-supplied role token (e.g. `data`, `web`,
    `dc`, `pki`).

### Why these specifics matter for the preclassifier

The reference fleet is sized to exercise every branch of the
preclassifier's grouping logic:

  - **PRIMARY partition** — three distinct vCenters never merge.
  - **Application sub-split by role** — EHRPro has 4 tiers; the
    preclassifier should produce 3 distinct EHRPro groups (web,
    app, data — workers fall into `app` via the worker pattern).
  - **Small-primary collapse** — EHRStaging (4 VMs) and EHRDR
    (3 VMs) collapse to one group each because they're below the
    5-VM threshold.
  - **Infrastructure pattern coverage** — IdentityServices uses
    `ad-dc`, `rhidm`, `pki` names; InfraServices uses `dns`, `ntp`,
    `backup`. All should resolve to role=`infrastructure` via the
    pattern matcher.
  - **Datastore tier signal** — gold/silver/bronze datastores
    mirror federal storage tiering. Mixed-tier groups (workers on
    bronze, app on silver) get empty `datastores` in the merged
    group's `shared_attributes` — which is correct behavior; the
    network attribute fills the cohesion role instead.

### Expected output

Running the preclassifier against the fleet produces **5-16 rich
groups** (under the 20-VM LLM ceiling). See
`tests/test_planner_e2e.py::test_preclassifier_57_vm_fleet_stays_under_llm_ceiling`
for the pinned invariant.

Group breakdown (approximate, deterministic):
  - EHRPro: 3 groups (web, app+worker, data)
  - PACSImaging: 3 groups (web, app, data+imaging)
  - IdentityServices: 1 group (infrastructure — AD-DC + RHIDM + PKI)
  - InfraServices: 1 group (infrastructure — DNS + NTP + Backup)
  - LegacyApp: 3 groups (web, app, data)
  - DevSan: 3 groups (web, app, data)
  - EHRStaging: 1 group (collapsed — small primary)
  - EHRDR: 1 group (collapsed — small primary)

Total: ~16 groups. The mid-teens is the natural answer for 3
vCenters × 8 apps × ~3 tiers; the architectural invariant is
"≤ `LLM_MAX_ITEMS_PER_CALL` (default 20)" and the fleet sits
comfortably under that.

---

## Related fixtures

  - `backend/tests/conftest.py` — `mock_vm_payload`,
    `mock_ssh_state`, `mock_ollama_verdict`, `mock_ollama_plan`.
    Lightweight per-test fixtures for the API + validation tests.
  - `scripts/generate-test-rvtools.py` — generates RVTools .xlsx
    files at arbitrary scale (used by the 1000-VM scale tests).
    Different concern: it tests the parser + import path, not the
    planner.

---

## Adding to the fleet

If a new test scenario needs a different shape:

1. **Prefer extending `sample_fleet.py`** with a new app definition
   if the scenario fits the federal-hospital frame.
2. **Build a one-off fixture** in your test file if it's a corner
   case (cycles in dependencies, single-VM plans, etc.). Keep the
   fixture small and local to the test that needs it.
3. **Don't ship binary test data** unless you need the full RVTools
   import-flow round-trip. The fixture path is faster and easier
   to review.

When extending `sample_fleet.py`, keep the same naming + metadata
conventions so the test invariants ("network populated", "role
detection finds the right tier") continue to hold.
