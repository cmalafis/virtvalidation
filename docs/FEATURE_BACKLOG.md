# VirtValidate feature backlog

_Assembled 2026-09-21 against `feat/rvtools-server-ingestion` @ `9204695`._

## How to read this

This is a **ranked execution order**, not a catalogue. `docs/FEATURE-GAP-ANALYSIS.md`
is the catalogue — 23 gaps with grounding, each proved against forklift
`v2.12.1`. This file does not restate those findings; it cites them as `GAP-nn`
and answers a different question: *given everything that shipped through
2026-09-20, what should be built next and in what order?*

It adds three sources the gap analysis does not cover:

- the **runtime security findings** from the 0.2.1 sandbox assessment, which
  were never folded into a backlog (referenced below as `SBX-n`),
- the **nine assessment rules that can never fire** today, and
- the **anti-features** — code that exists, is reachable from the UI, and does
  not do what it appears to do.

Every claim below cites a `file:line` verified in the working tree at
`9204695`. Tiers are ordered by what unblocks real use, not by severity label.

---

## Tier 0 — Nothing else counts until these land

These are the items that make the difference between "the safeguards work" and
"the safeguards are decorative." The product SSHes into production servers; a
control that can be bypassed by not using the path it guards is not a control.

### F-01 — Authenticate the appliance; stop trusting `x-actor`
**GAP-14, SBX-4. Size: L. Blocks: everything in the federal story.**

There is no authentication anywhere in `backend/app/api/`. The audit actor is a
client-supplied `x-actor` header, read in **54 places** across the API modules.
An unauthenticated HTTP client picks its own identity.

This is load-bearing in a way that is easy to miss: the production
authorization gate (`authorized_by` + `authorization_reason`, enforced in
`app/api/waves.py`), every `CommandAudit` row, and every `InferenceLog` actor
are all attributed to that header. The controls are real; the identity behind
them is not. Nothing else in the safeguard suite means anything until this is
attested.

Shape: an oauth-proxy sidecar (or equivalent), with the proxy-asserted identity
threaded into `_actor()` so the header becomes unreachable rather than
merely discouraged.

### F-02 — Bring SSH key revocation under the safeguard regime
**SBX-1, SBX-2, SBX-3. Size: M.**

`app/services/ssh_key_service.py:276-279` opens SFTP and rewrites the remote
`~/.ssh/authorized_keys`. That path never touches `app.core.ssh_guard.assert_read_only`
— the gate lives at `SSHCollector._run`, and this code does not use
`SSHCollector`. It writes no `CommandAudit` row, and neither revoke endpoint
checks the global kill-switch. In the 0.2.1 sandbox run this was **proven**: an
unauthenticated POST to `/api/ssh-keys/1/revoke-from-vms` returned 200 and
removed the key from a live host's `authorized_keys` **with the kill-switch
explicitly off**, producing zero audit rows.

CLAUDE.md states, in the present tense, that SSH commands are "a fixed
read-only set" gated "fail-closed" at "the single choke point." That is true of
the collector and false of the appliance.

The honest fix is not to claim no writes exist — key revocation is inherently a
write. It is to build a **sanctioned, audited write lane**: kill-switch check,
`CommandAudit` rows carrying an explicit `is_mutating` flag, and a safe write
ordering that cannot strand a host with no `authorized_keys` at all (SBX-2:
`ssh_key_service.py` removes before it verifies the replacement landed).

### F-03 — Make the host-key policy setting do something on the wave path
**SBX-5. Size: S.**

A security setting that silently does nothing is worse than an absent one — an
operator who sets it believes they are protected.

### F-04 — Expose the TrustyAI backend in the Helm chart
**SBX-12. Size: S.**

`trustyai` is a first-class backend in CLAUDE.md, has a backend implementation,
a `LLMGuardrailError` path, a `mechanical_fallback_guardrail` method value and
`detections` persistence on `InferenceLog` — and **zero occurrences** in
`deploy/helm/virtvalidate/values.yaml` or `templates/_helpers.tpl`. It cannot
be configured on a Helm-deployed cluster, which is every real deployment. The
guardrail feature is unreachable in production.

---

## Tier 1 — Make the product name true

VirtValidate plans *when* VMs move and checks *that* they arrived. The
validation half is the half the name promises, and it is the thinner half.

### F-05 — Deterministic validation verdicts
**GAP-08. Size: M.**

`app/core/validation.py:367-377` takes the model's `status` string and maps it
straight into `ValidationStatus`. The LLM decides pass/fail.

This contradicts the LLM Input Discipline rule in CLAUDE.md as directly as
anything in the codebase: pass/fail against a captured baseline is a
*comparison*, which is exactly the mechanical work the rule says to keep in
Python. It also leaves two validation stacks that can disagree — the
deterministic wave path and the per-VM LLM path — with no reconciliation.

Target shape: Python computes the verdict from the diff; the LLM writes the
explanation and the remediation hint. Same division of labour the planner
already uses for wave rationale.

### F-06 — Validate what MTV actually changes
**GAP-09. Size: L. The highest-value item in this file.**

Today's validation diffs a VM against its own pre-migration baseline. That
catches drift, but it does not check the specific things the migration itself
rewrites — which is where migration bugs actually live:

- virtio drivers bound (vs. the VM limping on emulated devices)
- `qemu-guest-agent` installed and running
- NIC renamed by the guest OS, and whether a static IP survived it
  (RHEL 9/10 NIC-naming changes are a documented static-IP loss path)
- `/etc/fstab` entries still resolving after the disk controller change
- VMware Tools remnants removed
- Windows ghost NICs from the old vNIC

The scaffolding is already there and unused: `app/core/commands.py:53-57`
declares `firewall_inspect`, `time_sync_status` and `package_list` for every OS
under a comment that calls them "anticipated future collection," and nothing
calls them.

Build this as a **declarative check catalog** rather than more prose prompts —
each check is a probe plus an expected post-migration condition, so the catalog
is reviewable by a customer and extensible without touching the engine.

### F-07 — Let the reporting layer see wave-scoped validation
**SBX-10, severity critical-correctness. Size: M.**

Validation runs performed through the wave path are invisible to the reports.
An operator who validates a wave and then opens Reports sees a different — and
wrong — picture of what has been checked.

---

## Tier 2 — Fidelity to MTV

`docs/MTV-GROUNDING.md` §13 tracks these. Two rows remain open from the
2026-09-20 emission work; the rest are newly actionable because the September
import work ingested the data they need.

### F-08 — Primary UDN needs `role` and `topology` on `TargetNetwork`
**GAP-04 remainder. Size: M.**

`app/models/target_network.py:31-35` still types the network as
`nad | cudn | udn | pod` only. MTV requires a primary UDN to be expressed as
`type: pod` in the NetworkMap (it detects the UDN via
`Plan.DestinationHasUdnNetwork` and sets `l2bridge` itself). Without a
role/topology distinction, VirtValidate cannot tell a primary UDN from a
secondary one, and emits `multus` for any non-pod catalog type.

Also in scope: emit `type: ignored` for deliberately-dropped NICs, which is
currently inexpressible.

### F-09 — Emit `volumeMode`
**GAP-05 remainder. Size: S.**

`volume_mode` appears **nowhere** in `backend/app` outside the vendored CRD.
`accessMode` landed in `719a6b6`; this is its other half. Relevant because RWO
does not support live VM migration, so the pair together determine whether the
migrated VM can live-migrate at all.

### F-10 — Real wave sizing
**GAP-06. Size: L. Design approved 2026-09-20; now unblocked.**

`app/core/wave_skeleton.py:76` hardcodes `MAX_VMS_PER_WAVE: int = 10`. The
grounding doc shows that is not an MTV limit. The real constraints are ≤10 VMs
per *ESXi host* per plan (beyond that, NFC memory must be raised), ≤500 disks
per plan, and a ~100-VM practical sweet spot — with a striking wall-clock
consequence: 8 × 100-VM plans complete in ~57 min where 1 × 500-VM plan takes
~5h10m.

`esxi_host` and `disk_count` are now ingested (`7fb9e2e`), so the inputs exist.
Add a cutover-window tier while touching this.

### F-11 — Co-plan shared-disk VMs
**GAP-18. Size: M.**

VMs sharing a disk must migrate in the same MTV plan (`migrateSharedDisks`).
The HA anti-affinity logic actively spreads them apart — the planner has no
notion of shared disks at all in `wave_skeleton.py`, `family.py` or `mtv.py`.
The assessment already *detects* them: the 1100-VM fixture contains 12.

### F-12 — Read three more RVTools sheets; light up nine dead rules
**GAP-15, GAP-16. Size: S per rule.**

`app/core/assessment.py:512` defines `_NEVER_EVALUATED` — nine MTV policies
that are reported as `not_evaluated` on every VM, forever, because the data
behind them is never read:

| Sheet needed | Rules unlocked |
|---|---|
| vUSB / vPCI | `vmware.tpm.detected`, `vmware.passthrough_device.detected`, `vmware.usb_controller.detected`, `vmware.device.sriov.detected` |
| vCluster | `vmware.cpu_affinity.detected`, `vmware.numa_affinity.detected`, `vmware.drs.enabled`, `vmware.dpm.enabled` |
| vSource + a FIPS flag on the target | `vv.fips.vsphere_version` |

The best-value-per-hour work in the repo: the parser already streams six
sheets, so each addition is one more sheet reader plus one rule body.

Do `vSource` first. vSphere 6/7 → a FIPS-enabled cluster is **unsupported by
MTV**, and a federal customer is exactly the customer who has both a FIPS
cluster and an older vCenter. Today VirtValidate would plan that migration
without a word.

---

## Tier 3 — Operator experience

### F-13 — Plan cancel and real progress
**GAP-20 remainder. Size: M.** The string `cancel` does not appear **once** in
`backend/app/api/plans.py`. A plan that starts cannot be stopped. The import
path already has a working cancel (`app/api/imports.py:228`) with
between-batch checks — copy its shape.

### F-14 — "Will it fit?" — cluster capacity on the target
**GAP-07. Size: M.** No capacity, allocatable or worker-count fields exist on
`app/models/target.py`. VirtValidate will happily plan a fleet into a cluster
that cannot hold it.

### F-15 — Bastion / ProxyJump
**GAP-11. Size: S.** Zero `proxyjump`/`bastion` handling in `app/core/ssh.py`.
This is newly urgent rather than merely convenient: a primary UDN breaks
`virtctl ssh` and `oc port-forward`, so the network architecture F-08 enables
is the same one that makes direct SSH unreachable.

### F-16 — Signed evidence bundle for an ATO conversation
**GAP-13. Size: M.** The zip has a README as of `719a6b6`. What an authorizing
official needs is the assessment, the authorizations, the validation results
and the command audit in one signed artifact.

### F-17 — Notifications
**roadmap-issue 06. Size: M.** Zero `slack|webhook|smtp|notify` anywhere in
`backend/app` or `frontend/src`. Lowest Tier-3 priority — real but not blocking.

---

## Tier 4 — Things that exist and lie

Cleanup, but the kind that costs trust when a customer finds it first. These
are cheap and should be batched into one pass.

- **Validation schedules never fire.** `app/core/scheduler.py:29` defines a
  single `_JOB_ID = "baseline-collection"` and the only `add_job` call is at
  `:89`. Meanwhile `app/api/validation_schedules.py` lets an operator create,
  pause and resume validation schedules that will never run. Either register
  the job or delete the API — the current state is the worst of both.
- **Inventory bulk capture fans out unthrottled.**
  `frontend/src/pages/InventoryPage.jsx:296-297` fires N concurrent
  `POST /api/vms/{id}/capture` through `Promise.allSettled`, against a backend
  that SSHes. `POST /api/snapshots/capture-bulk` exists and is already used by
  `BulkOperationsPage.jsx`. Select 500 VMs on the inventory page today and you
  aim 500 simultaneous SSH sessions at production.
- **Five endpoints have no frontend consumer**: `/api/planning-strategies`,
  `/api/templates/csv`, `/api/validations/run-all`, `/api/plans/preview-groups`,
  and the wave PDF report. Wire or delete.
- **`scripts/roadmap-issues/` is stale.** #01 and #07 are largely built; #02
  specifies WinRM + pywinrm for Windows support, but Windows shipped over
  OpenSSH + PowerShell (`app/core/commands.py:206-240`) and the gap analysis
  calls WinRM an explicit non-goal. The *content* of #02 — the Windows baseline
  schema (services, scheduled tasks, registry, AD membership, GPOs) — is still
  unbuilt and worth keeping; the transport is decided. #04, #05, #06 are
  genuinely unstarted.
- **CLAUDE.md's "Current status" is stale.** It checks off
  "Frontend dashboard (React) — VirtValidate.jsx", a file that no longer
  exists, and omits every capability shipped since August: RVTools server-side
  import, MoRef identity, migratability assessment, MTV YAML emission with
  offline CRD validation, resource mappings, OCP target catalogs, environment
  partitioning, design reviews, bulk operations, audit and inference logging.

---

## Observed on the 2026-09-21 sandbox run

The 1100-VM fixture was imported through the deployed appliance and a plan was
generated end to end. The pipeline held up: 8152 rows parsed, 1100 VMs created,
4 rejected with actionable reasons, assessment applied at import, 3 plans fanned
out by vCenter partition, 29 waves each with concurrency groups, and MTV YAML
that validates against the vendored CRDs. Paging `/api/vms` at `limit=1000`
returns 2.1 MB in 0.6 s. No scale problem was found.

One robustness item did surface, and it belongs in Tier 4:

### F-23 — Selection endpoints silently ignore unknown query parameters
**Size: S.**

`GET /api/vms` accepts `vcenter_source_id`. A request using the plausible-but-
wrong `source_vcenter_id` (the column name on the `VM` model is
`source_vcenter_id`, so the mismatch is easy to make) is not rejected — FastAPI
drops the unknown parameter and the endpoint returns the *unfiltered* set.

On a listing page that is merely surprising. On the path that feeds
`POST /api/plans` it is worse: a caller intending to plan one vCenter's 270
production VMs silently selects all 816 across three vCenters, and the plan is
accepted because it is still under `max_vms_per_plan`. The operator finds out
when three plans appear instead of one.

This is default framework behavior rather than a coding error, which is
precisely why it needs an explicit decision: reject unknown query parameters on
the selection endpoints, or at minimum echo the filters actually applied in the
list response so a caller can tell what it really asked for. Worth noting the
field name asymmetry too — the model column is `source_vcenter_id` and the
query parameter is `vcenter_source_id`.

---

## Deliberately not on this list

- **WinRM.** Decided against; Windows collection is OpenSSH + PowerShell.
- **vLLM backend implementation.** `app/core/llm/vllm_backend.py` raises
  `NotImplementedError` by design, targeted at v1.0.0, with settings already
  exposed so deployments need no config migration when it lands.
- **CI and security scanning.** Intentionally deferred per CLAUDE.md until
  there is a release cadence or contributors to protect. The dual-variant
  hardened image build is a product feature and remains supported.

## Sources

- `docs/FEATURE-GAP-ANALYSIS.md` — the 23-gap catalogue (`GAP-nn`)
- `docs/MTV-GROUNDING.md` — forklift v2.12.1 capability grounding
- `~/virtvalidate-private-notes/SANDBOX_DEPLOYMENT_ANALYSIS.md` — the 0.2.1
  live sandbox security assessment (`SBX-n`); findings 1-8 and 10-12 spot-checked
  as still open at `9204695`
