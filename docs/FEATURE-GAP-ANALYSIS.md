# Feature Gap Analysis — VirtValidate vs. what a VMware → OpenShift Virtualization migration requires

**Date:** 2026-09-20 · **Code baseline:** `b9493f8` · **Grounding:**
[`MTV-GROUNDING.md`](MTV-GROUNDING.md) (MTV 2.12 / forklift `v2.12.1`, OCP 4.22)

A finding is listed only if **both** halves hold: the code lacks it (cited
`file:line`), and a real migration needs it (cited grounding section `G§n`).
Every finding was checked against the no-cluster-auth rule; "Fit" says how.

Severity: **S1** produces a wrong/failed migration or blocks a federal
process · **S2** significant rework or late surprise · **S3** friction.
Effort: **S** ≤2 days · **M** ≤1 week · **L** >1 week.

## The one-paragraph finding

VirtValidate plans *when* VMs move and checks *that* they arrived, but it is
blind to *whether a VM can move at all and how*. It ingests one RVTools sheet
(`vInfo`) and keeps ~10 columns, discarding every field MTV's own validation
policies key on (G§10): disk mode, RDM, shared disks, snapshots, CBT, TPM,
firmware, passthrough, hot-add, guest OS id, ESXi host, MoRef. Consequently
the generated YAML identifies VMs incorrectly, forces warm migration on VMs
that cannot do it, and sizes waves by a limit MTV does not have. Most Tier 1
work is therefore one dependency chain rooted in ingestion.

---

## Findings

| ID | Gap | Derives from (grounding) | What goes wrong | Sev | Effort | No-cluster-auth fit |
|---|---|---|---|---|---|---|
| **GAP-01** | **No migratability assessment.** Nothing detects RDM, independent disks, shared disks, NVMe, passthrough, vTPM, snapshots/consolidation, CBT-off, hot-add, affinity, FT, unsupported guest OS, non-RFC1123 names, hibernation, Secure/Measured Boot. Zero hits for `RDM\|vTPM\|fault_toler\|independent` in `backend/app`; only `vInfo` is parsed (`frontend/src/utils/parseRVTools.js:236`); `assess_risk` (`core/preclassifier.py:291`) is a name/role heuristic. | G§10 — the 23 forklift VMware policies + documented limitations. 2 are Critical (cannot migrate), ~15 Warning. | Blockers surface in the MTV console on cutover night instead of during assessment — the exact failure this product exists to prevent. A VM with an independent disk or passthrough device sits in wave 3 looking healthy. | S1 | L | **Fits.** Pure RVTools data (`vInfo`, `vDisk`, `vSnapshot`, `vCPU`, `vMemory`, `vUSB`, `vNetwork`). Deterministic rule table mirroring [S-REGO] ids so findings match what MTV will later say. No LLM. |
| **GAP-02** | **VM identity is wrong in generated YAML.** No MoRef/UUID column exists (`models/vm.py`; zero hits for `moref`); `core/mtv.py:337` writes the vSphere display name into `vms[].id` and a slugified name into `vms[].name`. `VM.name` is globally unique (`models/vm.py:51`), so two vCenters with the same VM name collide. | G§4 — `id` is "the managed object ID"; `name` is the *source* qualified name. | MTV cannot resolve `id: "APP-DB-01"`; the slugified `name` (`app-db-01`) does not match the source either. **Generated plans likely resolve zero VMs.** | S1 | M | **Fits.** RVTools `vInfo` carries `VM ID` (MoRef) and `VM UUID` columns — stated from RVTools knowledge, not yet confirmed against a real export (the repo's fixture generator omits them, `scripts/generate-test-rvtools.py:67-73`); confirm on a customer file in Phase A. Ingest both; emit `id` when present, else source `name` only. Identity key becomes `(vcenter, moref)`. |
| **GAP-03** | **Warm is hardcoded; no per-plan/per-VM migration type and no prerequisite check.** `core/mtv.py:355` `warm: True`. | G§4 (`warm` deprecated → `type`), G§7 (warm needs CBT on VM + every disk, VMware Tools, Windows VSS; max 28 snapshots). | Every powered-off VM, every VM without CBT/Tools is sent down a path whose prerequisites it fails. Operator discovers at precopy. | S1 | S (after GAP-01) | **Fits.** Operator picks `cold\|warm` per plan; GAP-01 data drives a "N VMs in this wave cannot warm-migrate: CBT off" preflight. |
| **GAP-04** | **Network destination modelling conflates catalog type with MTV type, and unmapped networks silently become `pod`.** Catalog enum `nad/cudn/udn/pod` (`models/target_network.py:31`); `mtv.py:237-255` emits `multus` for anything not `pod` and falls back to `pod` when unmapped. No primary/secondary role on the catalog row. | G§2.1 — a **primary** UDN/CUDN must be `type: pod` (MTV detects UDN from the namespace); only secondary localnet CUDN / NAD are `multus`; `ignored` exists for dropped NICs. G§12 — primary UDN needs a namespace labelled at creation. | A VM mapped to a primary UDN gets a `multus` reference to a NAD MTV will not use that way; a forgotten DMZ portgroup silently lands on the pod network — a **security-zone breach that passes YAML validation**. | S1 | M | **Fits.** Declared metadata: add `role` (primary/secondary) + `topology` to `TargetNetwork`; derive MTV type. Unmapped → hard error or explicit operator-chosen `ignored`. |
| **GAP-05** | **StorageMap drops the operator's access mode; volume mode is not modelled.** `mtv.py:295` emits `storageClass` only, though `StorageMappingItem.access_mode` is collected (`schemas/target.py:81`). No `volume_mode` anywhere. | G§3 — `accessMode`, `volumeMode` valid; **RWO does not support live migration**. | Operator selects RWX for live-migratable prod VMs; YAML omits it; VMs land per StorageProfile default and cannot be live-migrated during node maintenance. | S2 | S | **Fits.** Declared metadata. Add `volume_mode`; emit both; warn on RWO for prod. |
| **GAP-06** | **Wave size is grounded on a limit MTV does not have; the limits it does have are ignored.** `MAX_VMS_PER_WAVE = 10` justified as "MTV per-ESXi concurrency" (`core/wave_skeleton.py:76`). ESXi host and disk count are not ingested. No hierarchy above waves. | G§8 — `max_vm_inflight` default **20 per ESXi host**; NFC constraint is **>10 VMs per ESXi host per plan**; **500 disks per plan**; ~100-VM plans are the documented sweet spot. | 1,000 VMs → ~100 Plan CRs per partition: unusable by a human, and no safer — 10 VMs on one ESXi host hit the NFC limit just as hard, while 60 VMs across 8 hosts would be fine. | S2 | L | **Fits.** Ingest `Host` + disk count. Constrain waves by **≤10 VMs per ESXi host, ≤500 disks, ≤~100 VMs**, and add a *cutover window* level above waves for human sequencing. Proposal only — needs sign-off before Phase 3. |
| **GAP-07** | **No "will it fit?"** `OCPTarget` holds no capacity (`models/target.py:49`); VM vCPU/memory/provisioned storage are not stored (`models/vm.py`). `capacity_concern` exists only as an LLM prose category (`core/storage_review.py:214`). | G§3 storage class is per-cluster; CNV schedules on requests; no MTV check exists for aggregate fit. | Wave 12 of 30 fails scheduling or fills the storage pool; months of sequencing replanned. | S2 | M | **Fits** as declared metadata (worker count × allocatable CPU/mem, per-StorageClass usable TiB, overcommit ratio) — optionally fed by the GAP-10 evidence pack (`oc get nodes -o json`). Cumulative per-wave fit curve, pure arithmetic. |
| **GAP-08** | **The LLM decides pass/fail on the per-VM validation path**, and two validation stacks disagree. `core/validation.py:367-378` maps the model's `status` straight to `ValidationStatus`; the wave-scoped stack (`api/waves.py:487`, `core/collection/diff.py:336`) is fully deterministic with different storage. | Not an MTV behaviour — a federal auditability requirement the project's own CLAUDE.md states, and Prompt R2 §8 ("the LLM never decides pass/fail"). | A VM can be `warn` in one screen and `pass` in another; a go/no-go verdict in a change record traces to model output. | S1 | M | **Fits.** Verdict = deterministic rules; LLM writes the explanation only. Collapse to one stack. |
| **GAP-09** | **Validation does not check the things MTV changes.** Collected: services, ports, mounts, cron, kernel, IPs (`core/ssh.py:202-239`). Not checked: virtio modules/drivers bound, `qemu-guest-agent`, NIC rename vs static IP, fstab device-path entries, initramfs drivers, VMware Tools remnants, Windows ghost NIC / offline disks / activation / pending reboot. `time_sync_status`, `firewall_inspect`, `package_list` are declared but never called (`core/commands.py:55-57`). Checks are Python constants, not data. | G§11 (virt-v2v injects VirtIO, installs guest agent at first boot, removes Tools, rewrites boot config, NIC names change); G§10 known issues (RHEL 9/10 NIC naming → static IP loss; Win 2012 R2 no virtio). | The product reports "pass" on a VM whose guest agent never installed or whose static IP sits on a ghost adapter — the well-known post-V2V failures. | S1 | L | **Fits.** Agentless SSH (already Windows-capable via OpenSSH+PowerShell). Declarative check catalog; every command still gated by `ssh_guard`. |
| **GAP-10** | **No cluster-side truth.** No upload handler exists in `backend/app/api/*`; nothing reads VM/VMI/DataVolume/PVC/Migration status. | G§9 (partial-failure semantics; `deleteVmOnFailMigration`), G§3 (what access/volume mode PVCs actually got). | Guest-level checks cannot see "DataVolume stuck", "VMI not live-migratable", "PVC is RWO". | S2 | M | **Redesigned to fit:** VirtValidate *generates* a read-only `oc get … -o json` script; operator runs it with their own credentials and uploads the archive. Same pattern feeds GAP-07. |
| **GAP-11** | **No bastion/jump-host.** No ProxyJump/sock support (`core/ssh.py`). | Segmented federal networks; migrated VMs on a primary UDN are not reachable via `virtctl ssh`/port-forward (G§12). | Validation cannot reach exactly the VMs federal sites isolate most. | S2 | S | Fits (paramiko `sock` channel). |
| **GAP-12** | **Re-import does not invalidate or reconcile plans.** Delta importer marks `missing_from_last_upload` (`core/rvtools_import.py:43`) but nothing flags plans whose VMs changed; and because GAP-01 fields aren't stored, a new snapshot/RDM/CBT change is invisible. | Migrations run for months; G§7 "do not snapshot after start"; G§10 consolidation. | Plan generated in week 1 executed in week 9 against a VM that has since gained an independent disk. | S2 | M | Fits. Per-VM assessment hash on the plan; "N VMs changed since this plan was generated" + re-assess without discarding human overrides. |
| **GAP-13** | **No change-record / ATO evidence bundle.** Per-artifact exports only; zip has no README/apply order (`api/plans.py:831`); the LLM wave PDF endpoint has no UI consumer (`ReportViewPage.jsx:60` prints HTML). | Federal change control requires an attachable artifact: what will run, who approved, what was checked, outcome. | Approval step done by screenshot. | S2 | M | Fits. One signed-hash bundle: assessment, plan, YAML + README with `oc apply --dry-run=server`, authorizations, validation results, audit excerpt. |
| **GAP-14** | **Identity is an unauthenticated header.** `x-actor` (`api/plans.py:647`); `authorized_by` is free text (`api/waves.py:195`); no users/roles; CORS pinned to localhost (`main.py:172`). | Audit records must be attributable (AU-family controls). | The audit trail the product advertises is self-asserted. | S1 (federal) | L | Fits — **trusted reverse-proxy header (OAuth-proxy / mTLS CN)**, not an in-app IdP; record the source of identity on every audit row. |
| **GAP-15** | **VMware constructs silently dropped.** DRS/host/NUMA/CPU affinity, reservations/limits/shares, resource pools, hot-add, tags, vApp ordering — none ingested or reported. | G§10 (affinity, hotplug, DRS, DPM, FT policies), G§4 (`targetAffinity`, `targetNodeSelector`, `tagMapping` exist to compensate). | Anti-affinity between two DB replicas vanishes; both land on one node. | S2 | M | Fits. RVTools `vInfo`/`vCluster`/`vRP`/`vTag`. "Considerations" section per wave; optionally emit `targetAffinity` for detected HA families. Depends on GAP-01 ingestion. |
| **GAP-16** | **FIPS target × vSphere version not checked.** No vSphere version or target FIPS flag stored. | G§10 — "cannot migrate vSphere 6 and vSphere 7 VMs to a FIPS-compliant cluster". | The defining federal combination fails at conversion. | S1 (federal) | S | Fits. Declared `fips_enabled` on `OCPTarget`; vCenter/ESXi version from RVTools `vSource`/`vHost`. One rule in GAP-01's table. |
| **GAP-17** | **Primary-UDN prerequisites not surfaced.** Namespace catalog has no "has primary UDN / labelled at creation" flag; provider-IP-inside-UDN-subnet not checked. | G§2.1, G§12. | Migration fails ("provider IP within UDN subnet") or VMs land on the wrong network. | S2 | S | Fits. Declared metadata on `OCPTargetNamespace` + subnet on the network row; arithmetic check against vCenter IP. |
| **GAP-18** | **Shared-disk VMs are not co-planned.** HA split deliberately *spreads* family members (`core/family.py`). | G§10/G§4 — shared disks need the two-plan strategy (`migrateSharedDisks` true for one plan, false for the rest). | A WSFC/Oracle RAC pair is spread over two waves and both try to migrate the shared disk. | S2 | M | Fits. Needs GAP-01 `vDisk` sharing mode. Planner constraint: shared-disk peers are a *together* group with explicit plan flags. |
| **GAP-19** | **No hooks / LUKS / Migration CR support.** Only NetworkMap, StorageMap, Plan emitted (`core/mtv.py:494`). | G§4 (`luks`, `nbdeClevis`, `hooks`), G§5, G§6. | Encrypted Linux VMs fail conversion; operators hand-edit every plan. | S3 | M | Fits. Secrets are referenced by name only — never held. Migration CR shipped as a separate, not-applied-by-default file. |
| **GAP-20** | **No plan cancel, no rationale re-run, flat progress, wizard follows 1 of N plans.** No cancel endpoint; stage 6 reports a constant 70% (`core/plan_generation.py:29-37`); `PlanWizardPage.jsx:201` polls only `plan.id` though `plans[]` is returned (`api/plans.py:307`); no per-call `wait_for` (`core/wave_annotation.py`). | Scale target (1,000 VMs → many LLM calls). | A multi-partition plan "completes" while siblings still run; a hung MaaS call holds a wave for the full HTTP timeout × 3. | S2 | M | Fits. |
| **GAP-21** | **RVTools import path is broken and lossy.** `RVToolsUploadPage.jsx:77-78` calls `.map` on an object/Promise; `RVToolsVMRow` (`schemas/vcenter.py:61`, `extra="ignore"`) drops `vsphere_cluster`/`vsphere_folder`/`custom_attributes`; `run_rvtools_import` never calls `detect_environment`, so imported VMs have `environment = NULL` and the environment partition is inert; per-row flush, single commit, in-memory task state. | Prerequisite for everything above. | The primary ingestion path does not work from the UI; prod and dev VMs partition together. | S1 | L | Fits. This is Phase 2, widened to multi-sheet server-side parsing. |
| **GAP-22** | **No project export/import.** | Hand-off between cleared staff / enclaves; air-gap transfer. | A migration lead's months of overrides are trapped in one appliance DB. | S3 | M | Fits (JSON bundle; no secrets, no keys). |
| **GAP-23** | **No rollback runbook.** Lifecycle has `rolled_back` state (`core/vm_lifecycle`) but no guidance artifact. | G§9/G§11 — source VM is powered off, not deleted; MACs preserved (duplicate-MAC risk on power-on); warm cutover is the point of no return for data. | Rollback improvised under pressure. | S3 | S | Fits. Deterministic per-wave text in the GAP-13 bundle. |

---

## Tier 1 — build next (ranked)

| # | Gap | Why here |
|---|---|---|
| 1 | **GAP-21** Server-side multi-sheet ingestion (fix the broken path, keep all columns, wire `detect_environment`) | Root of the dependency chain. Nothing below is possible from `vInfo`-only, and today's UI path is broken outright. Absorbs Prompt R2 Phase 2. |
| 2 | **GAP-02** MoRef/UUID identity + correct `vms[]` | Without it the YAML — the product's primary deliverable — likely resolves no VMs. Small once #1 lands. |
| 3 | **GAP-01** (+**GAP-16**) Migratability assessment mirroring forklift policy ids | The highest-value missing capability; it is the "validation" a buyer expects *before* migrating. Deterministic, auditable, zero LLM. |
| 4 | **GAP-03 + GAP-04 + GAP-05** MTV-correct emission: `type`, primary-UDN → `pod`, no silent pod fallback, access/volume mode, offline CRD schema validation | Each is a wrong-migration bug in shipped output. Absorbs R2 Phases 4-5. #3 supplies the warm-eligibility data. |
| 5 | **GAP-08** LLM out of the verdict path; one validation stack | Cheap, and it is a precondition for building #6 on a sound base rather than a third stack. |
| 6 | **GAP-09** Post-migration check catalog (declarative, Linux + Windows) | Makes the product name true. Absorbs R2 Phase 6 Path A. |
| 7 | **GAP-06** Wave sizing on real MTV limits + cutover-window hierarchy | Needs `Host` + disk counts from #1. **Requires a design decision from you before implementation** (see below). Absorbs R2 Phase 3. |

Sequencing: **1 → 2 → 3 → 4** is strictly ordered. **5 → 6** is an
independent track that can interleave. **7** needs only #1.

### Decision needed for GAP-06

Replace the fixed `MAX_VMS_PER_WAVE = 10` with constraints MTV actually
documents — **≤10 VMs per ESXi host per wave** (NFC), **≤500 disks per
wave** (MTV-1203), soft target **~100 VMs per wave** — and add a *cutover
window* grouping above waves so a human sequences ~10 windows, not ~100
waves. The LLM ≤10-items rule is unaffected: rationale is already per-wave
over *groups*, and groups stay capped at 10 per call. This changes a
constant that CLAUDE.md says needs "explicit operator approval", so it is
flagged rather than assumed.

## Tier 2 — worth building, not now

GAP-10 evidence pack · GAP-07 capacity fit (wants GAP-10 as an optional
feeder) · GAP-12 drift → plan invalidation · GAP-13 change-record bundle ·
GAP-15 dropped VMware constructs · GAP-17 UDN prerequisites · GAP-18
shared-disk co-planning · GAP-20 cancel / re-run / progress · GAP-11 bastion ·
GAP-14 proxy-asserted identity (S1 for federal, but L and orthogonal — it
should be scheduled deliberately, not squeezed in).

## Tier 3 — acknowledged, probably never

GAP-19 hooks/LUKS/Migration authoring (operators own secrets and cutover
timing; a documented hand-edit is adequate) · GAP-22 project export ·
GAP-23 rollback runbook (fold into GAP-13 if that is built).

---

## Anti-features (remove or fix)

| Item | Evidence | Action |
|---|---|---|
| `OCPTargetStatus` `inactive`/`error` are unreachable; `preflight.target_status` always `active` | nothing writes it (`api/targets.py`) | Stop surfacing it (schema removal is out of scope per R2 §12) |
| `preflight.missing_namespaces_on_target` hardcoded `[]` | `api/targets.py:707` | Compute it from the namespace catalog or drop the field |
| Validation schedules never fire | `core/scheduler.py:89` registers one unrelated job | Remove the API or implement it; today it promises unattended validation that never happens |
| ~10 endpoints with no UI consumer (`/api/planning-strategies`, `/api/templates/csv`, `/validations/run-all`, `/snapshots/capture-all`, `/plans/preview-groups`, wave PDF report, …) | audit | Wire or delete |
| Inventory "Capture baseline" fires N unthrottled requests; bulk endpoint exists unused | `InventoryPage.jsx:291-306` | Use `POST /api/snapshots/capture-bulk` |
| Inventory `Namespace` column reads a field not on `VMRead` — always "—" | `InventoryPage.jsx:442` | Use `resolved_target_namespace` |
| Selection `Set` persists invisibly across pages/filters | `InventoryPage.jsx:168` | Clear or show off-screen count |
| Search placeholder promises IP search; backend doesn't search IP | `InventoryPage.jsx:330` vs `api/vms.py:216` | Add `ip_address` to the `ilike` |
| `collector_spec.py` lists 14 probes `available=False`; `commands.py:55-57` ships uncalled commands | — | Superseded by GAP-09's catalog |
| Older RVTools endpoints (`/sources/vcenters/{id}/rvtools/*`) duplicate the multi-vCenter path with different async behaviour | `api/vcenters.py:304,431` | Collapse in GAP-21 |
| Stale docs: WinRM "future work" (`ARCHITECTURE.md:164,272`); `RECON_REPORT.md` "CI is red"; refs to nonexistent `PLAN_OVERRIDES.md`, `SECURITY.md`, `ocp_discovery.py`; `ENVIRONMENT_LABELS.md` self-contradiction | audit | Fix/regenerate |
| Repo cruft: `path/to/venv/`, `frontend/dist/`, `backend/.coverage` | — | Remove + gitignore |
| Registry defaults disagree (`quay.io/cmalafis10` chart vs `ghcr.io/virtvalidate` build script) | `values.yaml`, `scripts/build-images.sh` | Align |

Note: the "OCP target is inactive — discovery may be stale or auth has
expired" warning named in Prompt R2 is **already removed**
(`api/targets.py:656-660`).

## Already covered (so this analysis is falsifiable)

| A reviewer might expect a gap | It exists |
|---|---|
| Cluster credentials held by the appliance | Removed — `OCPTarget` is metadata-only (`models/target.py:49`); `ocp_discovery.py` deleted in `3c40884` |
| Per-cluster target network / storage / namespace catalogs with CRUD and FK-safe delete | `models/target_network.py`, `target_storage_class.py`, `ocp_namespace.py`; `api/target_entities.py` |
| Mapping dropdowns from declared targets; unmapped sources block YAML | `ResourceMappingDetailPage.jsx:549`; `mtv.py:455,463`; Stage 0 `mapping_validation.py` |
| Never mix vCenters in a wave | Enforced in `wave_skeleton.py:444-459` (a `continue`, not an assert — hardening belongs in GAP-06) |
| Async plan generation with polling; restart recovery | `api/plans.py:131` 202 + BackgroundTasks; `core/startup.py:54` |
| Bounded-concurrency LLM, validate-retry-fallback, per-wave `method`, rollup | `wave_annotation.py:578-610`; `plan_generation.py:226-241` |
| Deterministic skeleton with tests | `tests/test_wave_skeleton.py:162`, `test_preclassifier.py:309` |
| Idempotent delta re-import (created/updated/missing/unchanged) | `core/rvtools_import.py` |
| Environment partitioning + detection cascade | `core/environment.py:196` (but not wired to RVTools — GAP-21) |
| Read-only SSH enforcement, per-command audit, kill-switch, prod authorization gate, dry-run preview, per-vCenter circuit breaker | `core/ssh_guard.py:232`, `models/command_audit.py`, `api/waves.py:151-195`, `collection/orchestrator.py:139` |
| Windows guest collection | OpenSSH + PowerShell (`core/commands.py:235`) — no WinRM, by design |
| FIPS key gating; full inference capture; LLM auth/guardrail banner | `core/fips.py`; `models/inference_log.py`; `core/llm/status.py` |
| Server-side pagination/sort/filter/facets for VMs | `api/vms.py:285-443` |
| MaaS + KServe backends, runtime-switchable | `core/llm/maas_backend.py`, `kserve_backend.py`, `runtime.py:116` |
| Wave PDF report | `core/reporter.py:514` (backend only; no UI consumer) |

---

## Proposed merged build plan (replaces R2 Phases 2-9 ordering)

| Phase | Content | Source |
|---|---|---|
| **A** | Server-side streaming multi-sheet RVTools ingestion + job table + reject report + idempotent `(vcenter, moref)` identity + `detect_environment` + indexes + 1,000-VM fixture with blockers and malformed rows | R2 Ph2 + GAP-21 + GAP-02 (ingest half) |
| **B** | Migratability assessment: rule table mirroring forklift policy ids, per-VM findings, inventory filters, plan-time gate, FIPS × vSphere rule | GAP-01, GAP-16 |
| **C** | MTV-correct emission: `vms[]` identity, `type`, network role → destination type, no silent fallback, access/volume mode, bundle README with `--dry-run=server`, **offline validation of every document against the vendored v2.12.1 CRD schemas**; mapping bulk ops + templates | R2 Ph4-5 + GAP-02/03/04/05 |
| **D** | Planner: host/disk-aware wave constraints, cutover windows, consolidation pass, hard vCenter assertion, per-call timeout, cancel, rationale re-run, real progress, wizard follows all N plans | R2 Ph3 + GAP-06 + GAP-20 *(needs the GAP-06 decision)* |
| **E** | Validation: single deterministic stack, LLM explain-only, declarative Linux + Windows check catalog with remediation, fixtures of real command output, bastion | R2 Ph6 Path A + GAP-08/09/11 |
| **F** | Evidence pack (generated read-only script → upload → parse), capacity fit | R2 Ph6 Path B + GAP-10/07 |
| **G** | UI guided flow, progress/cancel everywhere, large-list ergonomics (select-all-matching-filter), anti-feature cleanup — on **PatternFly 6** | R2 Ph7 |
| **H** | Scale/invariant/parser tests, `docs/TESTING.md`, sandbox deploy, tag | R2 Ph8-9 |

Every schema change follows the CLAUDE.md Alembic workflow; any new LLM flow
follows LLM Input Discipline (≤10 items/call, validate-retry-fallback, mock
backend intent). None of Phases A-C introduces an LLM call.
