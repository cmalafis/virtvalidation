# VirtValidate user guide

End-to-end, from an empty appliance to a validated migration wave.

This is the operator's guide. It assumes VirtValidate is already deployed
— see [INSTALLATION.md](INSTALLATION.md) for Podman/Quadlet and
[deploy/README.md](../deploy/README.md) for the Helm chart.

The interface is organized in the order you actually work:

| Section | What lives there |
|---|---|
| **Overview** | Fleet counts, setup progress, system health |
| **Configure** | vCenter sources, OpenShift targets, resource mappings |
| **Discover** | Virtual machines, design reviews |
| **Migrate** | Migration plans, bulk operations |
| **Verify** | Validations, reports |
| **Administration** | Agent activity, audit log, settings |

Work top to bottom. The Overview page tracks which setup steps are done
and links to the next one.

---

## 1. Before you begin

**What VirtValidate does.** It records what your VMs looked like on
VMware, then checks that they still look right after migrating to
OpenShift Virtualization. It plans the migration into waves and emits
the MTV (Forklift) YAML, but it does not perform the migration itself.

**What it needs.**

- **SSH reachability** from the appliance to every VM you want to
  validate, on the VM's SSH port. VirtValidate authenticates with keys it
  generates and holds; the private key never leaves the appliance.
- **An inventory export** — an RVTools `vInfo` sheet is the usual source.
  CSV and manual entry also work.
- **Knowledge of your target cluster** — its networks, storage classes,
  and namespaces. VirtValidate does **not** connect to the OpenShift
  cluster and stores no cluster credentials. You describe the cluster;
  it uses that description to emit manifests.

**Network posture.** Nothing leaves your network. Inference runs against
whichever backend you configure — a local Ollama, an in-cluster KServe or
vLLM, or a governed Model-as-a-Service endpoint inside your perimeter.
For air-gapped sites, simply don't configure the external option. Fonts
and assets are bundled into the image; the browser makes no third-party
requests.

**A note on read-only.** Every command VirtValidate runs on a managed
host is read-only, gated at a single choke point, and recorded. You can
see exactly what would run before anything connects — see
[§8](#8-run-a-wave) — and you can stop all SSH activity instantly with the
kill-switch in Settings.

---

## 2. First run

### Generate the appliance SSH key

**Settings → Compliance → Generate key.**

This creates the keypair VirtValidate uses to reach managed VMs. Copy the
public key; you'll distribute it in the next step.

If your site runs FIPS mode, the same tab reports whether FIPS is
*configured*, *detected on the host OS*, and *effective*. Configured but
not detected is the classic misconfiguration — the application-level gates
are on, but the crypto underneath isn't FIPS-validated.

### Distribute the public key

Add it to `~/.ssh/authorized_keys` on each VM you plan to validate. See
[SSH_SETUP.md](SSH_SETUP.md) for the sudoers hardening that keeps the
account read-only, and [WINDOWS_SETUP.md](WINDOWS_SETUP.md) for Windows
Server.

For anything beyond a handful of hosts, use the generated Ansible
playbook rather than doing this by hand — see [§7](#7-capture-baselines).

### Choose and test an inference backend

**Settings → Inference.**

Every backend is listed with whether it's configured and, if not, exactly
which environment variables are missing. Pick one and use **Test
connection** to confirm it answers before you rely on it.

If an LLM call later fails authentication, a red banner appears at the top
of Settings. That banner is deliberate: an auth failure otherwise shows up
only as quietly worse plan annotations.

---

## 3. Register your vCenter sources

**Configure → vCenter Sources → Register vCenter.**

Two fields matter beyond the name:

- **Hostname** must match the hostname in your RVTools export. That's how
  imported VMs attach to the right source.
- **Classification** — `cui` or above means every wave touching this
  source requires a named authorizer and a written reason before it can
  run. That gate is enforced by the API, not just the UI.

Migration plans never span vCenters, so each source you register is also
a plan partition boundary.

---

## 4. Register your OpenShift targets

**Configure → OCP Targets → Register target.**

Then open the target and fill in its three catalogs:

- **Networks** — the NADs (or UDN/CUDN) migrated VMs will attach to.
- **Storage classes** — with their access modes. Live migration between
  nodes needs `ReadWriteMany`.
- **Namespaces** — where VMs land. Plans partition by target namespace,
  so this also shapes how waves are grouped.

These catalogs are what plan generation resolves against. A gap here
surfaces later as a plan-generation failure with a per-VM report, so it's
worth completing now.

---

## 5. Import your inventory

**Discover → Virtual Machines → Import VMs.**

Select an RVTools `.xlsx` (the `vInfo` sheet) or a CSV. Parsing happens in
your browser; only the parsed rows are sent to the appliance.

The import then shows every vCenter hostname it detected and lets you
route each one to a registered source. **VMs whose hostname doesn't route
anywhere are skipped, not guessed at** — the count is shown before you
commit. If you see skipped VMs, either route them or go register that
vCenter first.

See [RVTOOLS_GUIDE.md](RVTOOLS_GUIDE.md) for large or multi-vCenter
estates, and [INVENTORY_GUIDE.md](INVENTORY_GUIDE.md) for the full CSV
column list.

### Label environments

Plans partition production separately from development and DR, because
operators sequence those stages separately in practice. Environment is
auto-detected from VM name, folder, cluster and custom attributes where
possible; you can correct it in bulk from the inventory table.

See [ENVIRONMENT_LABELS.md](ENVIRONMENT_LABELS.md).

---

## 6. Build resource mappings

**Configure → Resource Mappings → Create mapping.**

A mapping translates one vCenter's networks and datastores into one
cluster's NADs and StorageClasses. Open it and fill in the rows.

**Suggest with AI** proposes matches with a confidence and a rationale for
each row. These populate the form only — **nothing is saved until you
press Save.** Review them. A wrong network mapping puts a production VM on
the wrong VLAN.

**Run preflight** when you're done. It reports what's still unresolved:
unmapped networks, unmapped datastores, and anything mapped to a target
that isn't in the cluster's catalog. Plan generation performs the same
check and refuses to proceed with gaps, so a clean preflight here saves a
failed generation later.

See [RESOURCE_MAPPING.md](RESOURCE_MAPPING.md).

---

## 7. Capture baselines

A baseline is the pre-migration state that validation compares against.
Without one, validation has nothing to diff.

**Capture several over a few days.** A single snapshot can't distinguish
normal variation (a cron job that runs nightly, a service that restarts
weekly) from migration damage. Three to seven days is the usual window.
Set the cadence in **Settings → General → Baseline schedule**.

To capture on demand:

- **One VM** — open it from the inventory and use **Capture baseline**.
- **Many** — **Migrate → Bulk Operations → Capture baselines**. Select by
  stacking scope axes (vCenter, environment, application) rather than
  ticking rows, which is what makes this workable at a thousand VMs.

Every command run is visible afterwards in **Administration → Agent
Activity**.

---

## 8. Optional: review the design first

**Discover → Design Reviews.**

Paste or upload your proposed OpenShift network or storage design, and
VirtValidate analyzes it against what's actually in the source estate.
Findings carry a severity, a confidence, and the specific source and
proposed evidence behind them, so you can act on them or dismiss them
with a reason.

This is optional, but it is much cheaper than discovering the gap during
a cutover window.

See [DESIGN_REVIEW.md](DESIGN_REVIEW.md).

---

## 9. Generate a migration plan

**Migrate → Migration Plans → Generate plan.**

Three steps: name it, choose the resource mappings that cover the estate,
select the VMs. Only VMs not already committed to another plan appear.

The cap is 1,000 VMs per plan (`max_vms_per_plan`, adjustable at deploy
time). Well before that ceiling it's worth splitting anyway — one enormous
plan is harder to review and to sequence than several scoped ones, and the
wizard shows how many you've selected as you go.

Generation runs a pipeline and reports which stage it's on. Most stages
are deterministic and take under a second; the per-wave rationale step
calls the LLM and dominates the wall clock.

The result is a set of waves. Each wave is:

- at most 10 VMs, with at most 2 members of the same HA family, so a
  cluster never loses quorum to a single wave;
- coherent within one partition, so it maps to exactly one MTV Plan CR;
- tagged with a **parallel group** — waves sharing a group ID are safe to
  run at the same time.

Each wave also carries a **method** label. `LLM` means the model wrote the
rationale; `Deterministic fallback` means it didn't and Python did.
`Fallback — LLM auth rejected` and `Fallback — guardrail fired` mean
something specific went wrong and is worth investigating.

See [PLANNING_GUIDE.md](PLANNING_GUIDE.md) and
[HA_MIGRATION_STRATEGY.md](HA_MIGRATION_STRATEGY.md).

---

## 10. Run a wave

Open the plan. Each wave card has an execution panel.

### Dry run first

Expand **Dry run**. It lists the exact hosts that would be contacted and
the exact read-only commands that would run. **Nothing is contacted to
produce this.** Read it before your first wave on any new estate.

The panel also tells you up front if:

- the **SSH kill-switch is off**, in which case runs would be refused;
- the wave **requires named authorization**, because it targets
  production VMs or a CUI-or-above source. You'll be asked for an
  authorizer and a reason, both recorded permanently on the run.

### Capture, migrate, validate

1. **Capture baseline** for the wave, if you haven't already.
2. **Download MTV YAML** and apply it with Forklift/MTV. VirtValidate does
   not perform the migration.
3. Once the VMs are running on OpenShift, **Validate wave**.

Validation SSHes into each VM, collects the same state as the baseline,
diffs them, and has the LLM explain what changed and whether it matters.

---

## 11. Read the results

**Verify → Validations** shows fleet progress and per-VM verdicts. Open a
VM to see its findings, each with severity, confidence, and the evidence
behind it.

**Verify → Reports** generates five reports from live data:

| Report | Use it for |
|---|---|
| Executive summary | Leadership updates |
| Full validation report | The CISO-ready artifact |
| Failed and degraded VMs | Working a remediation list |
| Migration wave plan | Sequencing review |
| Pre-migration baseline snapshot | The evidence validation compares against |

Export by printing — **Print / save as PDF**. The print stylesheet drops
the app chrome and flattens to ink on paper.

---

## 12. Close out a wave

1. **Mark succeeded** on the plan, which moves its VMs to the `migrated`
   lifecycle state.
2. **Revoke the validation key** from the wave's VMs. Access was scoped to
   this wave; don't leave it in place.

If a migration has to be reversed, the inventory offers
**Revert to VMware** (`migrated` → `rolled_back`) and then **Make
available** (`rolled_back` → `available`) so those VMs can join a new plan.

---

## 13. Ongoing

- **Scheduled re-validation** catches drift after cutover.
- **Administration → Agent Activity** holds every SSH command the
  appliance ran and every LLM call it made, including full inputs,
  outputs, and any guardrail detections.
- **Administration → Audit Log** records every mutation.

---

## Troubleshooting

**"No VMs available to plan."** Every VM is already committed to a plan,
or inventory is empty. Delete a plan to release its VMs, or import more.

**Plan generation fails at the mapping stage.** A selected VM's network or
datastore doesn't resolve. Open the mapping and run preflight; the report
names the gaps.

**Baseline or validation returns 503.** The SSH kill-switch is off.
Settings → General.

**A wave refuses to run with a 422.** It needs named authorization —
production VMs or a CUI+ source. Provide the authorizer and reason in the
run dialog.

**Wave rationale says "Deterministic fallback".** The LLM didn't produce
usable output and Python did the work instead. The plan is valid, just
less nuanced. If the label says *auth rejected* or *guardrail fired*,
check Settings for the error banner.

**Everything shows "Backend unreachable".** The API isn't answering. Check
the backend container and `GET /api/health/full`.

**Collection fails on every host after rotating the appliance key.**
Managed VMs still have the old public key. Distribute the new one.

**The UI spins and never loads.** Requests are bounded at 60 seconds and
then report a timeout. If you see a timeout rather than a spinner, the
backend is reachable but slow — check its logs.

---

## Where to go next

- [PLANNING_GUIDE.md](PLANNING_GUIDE.md) — choosing a planning strategy
- [INVENTORY_GUIDE.md](INVENTORY_GUIDE.md) — the inventory table in depth
- [RESOURCE_MAPPING.md](RESOURCE_MAPPING.md) — mapping semantics
- [SSH_KEY_GUIDE.md](SSH_KEY_GUIDE.md) — key lifecycle
- [VALIDATION_SCALING.md](VALIDATION_SCALING.md) — 100 to 10,000 VMs
- [FIPS_DEPLOYMENT.md](FIPS_DEPLOYMENT.md) — federal deployments
- [DEMO_WALKTHROUGH.md](DEMO_WALKTHROUGH.md) — a narrated end-to-end run
