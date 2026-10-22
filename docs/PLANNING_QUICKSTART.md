# Planning quickstart: RVTools → migration waves → MTV YAML

This is the path for teams that want VirtValidate's **migration planner** and
nothing else: import an RVTools export, let the planner build waves, adjust the
waves by hand, and download MTV (Forklift) YAML. You do **not** need SSH access
to any VM, baselines, or post-migration validation for any of this.

The appliance never connects to vCenter or to OpenShift. Everything it knows
comes from the RVTools file and from what you tell it about the target cluster.

| Step | Where | Time |
|---|---|---|
| 1. Register each vCenter | Configure → vCenter Sources | 1 min |
| 2. Import the RVTools export | Discover → Virtual Machines → Import VMs | 1–5 min |
| 3. Check environment labels | Discover → Virtual Machines | 5 min |
| 4. Register the OpenShift cluster | Configure → OCP Targets | 5 min |
| 5. Map networks, datastores, namespaces | Configure → Resource Mappings | 15–60 min |
| 6. Generate plans | Migrate → Migration Plans → Generate plan | 1 min |
| 7. Review and edit waves | the plan's page | as needed |
| 8. Download the YAML and apply it | the plan's page | — |

---

## What you can skip

Settings → Compliance (the SSH key), Validations, Reports and Bulk Operations
are for the validation workflow. Leave them alone.

**The LLM is optional for planning.** Wave structure is decided by
deterministic Python rules; the LLM only writes each wave's plain-English
rationale and proposes mapping matches. If no inference backend is reachable,
every wave still gets a rationale, written by the deterministic fallback, and
its **method** label reads *Deterministic fallback* instead of *LLM*. The plan
and the YAML are equally valid either way.

---

## 1. Register each vCenter

**Configure → vCenter Sources → Register vCenter.**

Only the name and **hostname** matter here. No credentials are asked for or
stored. The hostname must match the **VI SDK Server** column of the RVTools
export (for example `vcenter01.corp.example`). This is how imported VMs are
attached to their vCenter.

Two side effects to know about:

- **The name is used in the YAML.** The generated Plan references a source
  Provider CR with the vCenter's name in VirtValidate. Name it the same as the
  vSphere Provider you create (or already have) in MTV, or rename one of them
  later.
- **A vCenter is a plan boundary.** A plan never spans vCenters.

## 2. Import the RVTools export

**Discover → Virtual Machines → Import VMs.**

In RVTools use **Export all to Excel** and upload the `.xlsx`. Re-save a
`.xls` as `.xlsx` first. The file is parsed on the appliance.

| Sheet | Required | What it adds |
|---|---|---|
| `vInfo` | yes | the VM list: name, MoRef, power state, CPU/memory, OS, folder, cluster, networks, datastore path, annotations |
| `vNetwork` | no | every NIC's port group (more complete than `vInfo`'s columns) |
| `vPort` / `dvPort` | no | port-group VLAN IDs, which let **Suggest mappings** match networks by VLAN |
| `vDisk` | no | per-disk datastores; detects RDM, independent and shared disks |
| `vSnapshot`, `vCPU`, `vMemory` | no | snapshot, hot-add and CPU/memory findings |

A `vInfo`-only export imports fine. The disk-level blockers just can't be
assessed from it.

After the upload, VirtValidate lists every vCenter hostname found in the file.
Route each one to a source registered in step 1. **Hostnames you don't route
are skipped, not guessed.** Then start the import.

When it finishes, the report lists problem rows. Download them as CSV from
the same page.

- **Rejected rows** were not imported: no name, templates, SRM placeholders,
  duplicates.
- **Warning rows** were imported but flagged: no datastore, no network.

Importing the same file again updates VMs in place. VMs are matched by MoRef,
so renamed VMs keep their row.

Every VM also gets a **migratability assessment** at import, for example: RDM
disks, a missing datastore, CBT disabled (warm only), NVMe controllers.
Blocked VMs are not planned. The wizard lists them and the reason.

Limits: 100 MB per file, 10,000 VMs per import, one import at a time. See
[RVTOOLS_GUIDE.md](RVTOOLS_GUIDE.md) for column aliases and large estates.

## 3. Check environment labels

Plans never mix **production** with **development / test / staging / DR**.
Every plan is cut by vCenter, target namespace and environment.

The environment is detected at import. Detection checks, in order:

1. an `Environment` custom attribute or annotation (`Environment: prod`)
2. the VM's folder
3. the vSphere cluster name
4. a name prefix such as `prd-` or `dev-`

What can't be detected is `unknown`. Check the **Environment** column in the
VM list before planning. Unknown VMs plan together as their own group, and the
*one namespace per environment* strategy (step 5) gives them no namespace.

There is no screen for editing environment labels yet. Use the API. Labels
you set this way are marked as set by hand and survive later re-imports.

```bash
# one label for many VMs
curl -X POST https://<appliance>/api/vms/bulk-set-environment \
  -H 'Content-Type: application/json' \
  -d '{"vm_ids": [12, 13, 14], "environment": "production"}'

# re-run detection after fixing folders/attributes in vSphere and re-importing
curl -X POST https://<appliance>/api/vms/redetect-environment \
  -H 'Content-Type: application/json' -d '{"dry_run": true}'
```

See [ENVIRONMENT_LABELS.md](ENVIRONMENT_LABELS.md).

## 4. Register the OpenShift cluster

**Configure → OCP Targets → Register target.** Give it a name and the API URL.
Set the OpenShift and MTV versions too: version-specific checks (UDN, copy
offload, EVPN) depend on them.

**The name is used in the YAML.** It is the destination Provider CR name, so
match it to the OpenShift Provider in MTV (usually `host`).

Then fill the cluster's catalog of networks, storage classes and namespaces.
The fastest way is **Import from cluster**. It shows a command to run with
your own `oc` login:

```bash
{ oc get network-attachment-definitions,userdefinednetworks -A -o yaml
  echo ---
  oc get clusteruserdefinednetworks,storageclasses,storageprofiles,nodenetworkconfigurationpolicies -o yaml
  echo ---
  oc get namespaces -l k8s.ovn.org/primary-user-defined-network -o yaml; } > cluster-catalog.yaml
```

Paste the contents of `cluster-catalog.yaml` into the dialog. **Preview** is a
dry run that shows what would be added or changed. **Apply** writes it, but
never deletes or blanks anything the paste doesn't mention.

You can also enter networks, storage classes and namespaces by hand.

## 5. Map networks, datastores and namespaces

**Configure → Resource Mappings → Create mapping**, one per
(vCenter, cluster) pair. Open it, then:

1. **Pull from inventory.** This adds a row for every port group and
   datastore the imported VMs use.
2. **Fill in the network rows.** Pick a target for each port group, or mark
   it **ignored** if those NICs should not be carried over.
3. **Fill in the storage rows.** Pick a StorageClass for each datastore. The
   access mode comes from the catalog. Live migration later needs
   `ReadWriteMany`.
4. **Optional: Suggest mappings.** It proposes matches by VLAN or name and
   asks the LLM about the rest. Suggestions only fill the form. **Nothing is
   saved until you press Save.**
5. **Set the namespace strategy:**
   - one namespace for everything
   - one per environment (fill in the table)
   - one per application, named `<prefix>-<application>-vms`
6. **Save, then Run preflight.** It reports unmapped sources and
   version-specific platform problems.

The mapping is **complete** when every source is mapped or ignored. A VM that
uses an unmapped network or datastore, or has no namespace, is skipped at
planning time with the exact gap named.

See [RESOURCE_MAPPING.md](RESOURCE_MAPPING.md).

## 6. Generate plans

**Migrate → Migration Plans → Generate plan.** The wizard has three steps.

1. **Name and migration type.**
   - **Cold**: VMs are shut down for the copy.
   - **Warm**: disks pre-copy while the VMs run, then a short cutover.

   Warm requires Changed Block Tracking, so VMs without it are skipped for a
   warm plan.
2. **Resource mappings.** Every mapping is ticked. Leave them ticked; each
   VM is resolved through the mapping for its own vCenter. Unticking all of
   them leaves nothing to resolve against, and the wizard won't continue.
3. **Select VMs.** Only VMs not already in a plan are shown. Filter by vCenter,
   environment or name, then tick rows or **Select all N matching**. There is
   no limit on how many you select.

Before you press **Generate plan**, the wizard shows:

- **The exact plans that will be created.** A selection is split by
  (vCenter, cluster, namespace) and cut into batches of at most 1,000 VMs.
  The batch size is `MAX_VMS_PER_PLAN`, set at deploy time. A 2,400-VM
  selection over one partition becomes `name-1`, `name-2` and `name-3`. A
  selection spanning partitions keeps the partition in each name, for
  example `name-vc01-ocp-east-prod-1`.
- **Every VM that will be skipped, and why.** Typical reasons are an
  assessment block for the chosen migration type, an unmapped network or
  datastore, or no target namespace. Skipped VMs stay available, and each
  plan's page lists them again.

  To bring a skipped VM in later, fix the cause (usually a mapping row),
  open the plan and use **Add VMs**. Or include it in the next plan.

Generation takes seconds for the structural steps. If an LLM is configured,
writing one rationale per wave runs in parallel and dominates the time.

### How waves are built

These rules are deterministic, the same input always gives the same waves,
and the LLM never changes them:

- **At most 10 VMs per wave.**
- **At most 2 members of one HA family per wave**, so a clustered application
  never loses quorum to one wave. Families are detected from names, for
  example `sql-01` and `sql-02`.
- **One partition per wave** (vCenter, cluster, namespace), so each wave is
  exactly one MTV Plan CR.
- VMs that share an application, network, datastore or role are kept
  together where the limits allow.
- **Parallel group:** waves with the same group ID touch different vCenters
  and different HA families, so they are safe to run at the same time.

Each wave card shows its VMs, a risk score, the rationale, where it lands
(network and storage summary, platform findings) and the method that wrote
the rationale.

## 7. Review and edit waves

The planner can't see dependencies that aren't in the RVTools data. On the
plan's page you can change any `complete` plan.

- **Find a VM** searches the whole plan. **All VMs** shows one table with a
  Wave column.
- Tick VMs; the selection can span waves. Then choose one of:
  - **Move to wave…**: into an existing wave.
  - **New wave from selection**: a new wave, numbered after the highest one.
  - **Move to plan…**: into another plan with the same vCenter, cluster and
    namespace (for example the next batch of a series), into an existing or a
    new wave.
  - **Remove from plan**: the VMs become available again.
- **Add VMs** brings available VMs from the same vCenter into a wave.

Every edit is dry-run first. The dialog shows what will move and what rules
it would break before you commit:

| Result | Examples | What you do |
|---|---|---|
| **Refused** | different vCenter/cluster/namespace, unmapped network, assessment-blocked VM, would empty the plan | fix the cause; can't be overridden |
| **Confirm** | wave over 10 VMs, too many HA peers in one wave, plan over the batch size | tick to confirm; the wave is labelled **Rule overridden** and the override is audited |
| **Info** | — | nothing |

Notes:

- **Wave numbers never change.** An emptied wave disappears and its number is
  not reused. YAML you already applied (`vv-plan-<id>-wave-<n>`) still refers
  to the same wave.
- **Removals can raise a confirmation too.** The HA cap depends on the
  family's total size in the plan, so removing a family member can tighten
  the cap on a wave you didn't touch.
- **Rationales for edited waves are rewritten in the background.** Until
  then they show a deterministic note.
- **The YAML always reflects the current waves.** It is generated when you
  download it, never stored.
- Every edit is in **Administration → Audit Log**.

## 8. Download the YAML and apply it

On the plan's page:

- **Download MTV YAML** on a wave card gives `plan-<id>-wave-<n>.yaml`.
- **Download all YAML** gives `plan-<id>-mtv-bundle.zip`: one file per wave
  plus a README with the exact commands.

If something can't be emitted, for example a mapping row edited since the
plan was generated, the download shows the reason instead of a file.

Each wave file holds three documents for namespace `openshift-mtv`:

1. a **NetworkMap**: source port group → NAD / UDN / pod network / ignored
2. a **StorageMap**: datastore → StorageClass, with access and volume mode
3. a **Plan** `vv-plan-<id>-wave-<n>`: the wave's VMs by MoRef, target
   namespace, cold or warm, and the rationale as its description

A wave whose VMs have no network in the export gets an empty NetworkMap. That
is valid. If those VMs do have NICs, MTV reports them as unmapped when the
Plan is validated, before any disk is copied.

**Before applying**, on the cluster:

- The MTV operator is installed.
- The **Provider CRs exist** and their names match the vCenter and target
  names from steps 1 and 4. VirtValidate never sees vCenter credentials, so
  you create these yourself.
- Every target namespace, NAD and StorageClass the maps name exists.

**Then**, wave by wave in order:

```bash
oc apply --dry-run=server -f plan-1-wave-1.yaml   # validates, changes nothing
oc apply -f plan-1-wave-1.yaml                    # creates the Plan; does not start it
oc get plan -n openshift-mtv vv-plan-1-wave-1 -o jsonpath='{.status.conditions}'
```

Start the migration from the MTV console or with a `Migration` CR. The bundle
README has the snippet.

## 9. Close out a plan

When the plan's waves have migrated, press **Mark succeeded** on the plan.
**No validation is required.** The plan becomes read-only, and its VMs move
from `planned` to `migrated` so they never appear in a new plan.

| Lifecycle state | Meaning |
|---|---|
| `available` | in no plan; can be selected |
| `planned` | in a plan |
| `migrated` | its plan was marked succeeded |
| `rolled_back` | moved back to VMware after migrating |

Deleting a plan returns its VMs to `available`.

There is no button yet to undo a migration. To roll a VM back and make it
plannable again, call the API twice:

```bash
curl -X PATCH https://<appliance>/api/vms/<id> -H 'Content-Type: application/json' \
  -d '{"lifecycle_state": "rolled_back"}'
curl -X PATCH https://<appliance>/api/vms/<id> -H 'Content-Type: application/json' \
  -d '{"lifecycle_state": "available"}'
```

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Import skipped VMs | their vCenter hostname wasn't routed: register the vCenter (step 1) and import again |
| Wizard says every VM is skipped | read the reasons. Usually mappings are incomplete (step 5) or the VMs are blocked for the chosen migration type |
| A VM with no datastore is skipped | the export has no datastore for it; re-export with **Export all** (the `vInfo` Path column and `vDisk`) |
| Prod and dev VMs ended up in one plan | they share an environment label; fix the labels (step 3) and re-plan |
| `oc apply` can't find a Provider | Provider CR names must equal the vCenter and target names in VirtValidate |
| Waves say *Deterministic fallback* | no LLM was reachable; the plan is still valid. Configure one under Settings → Inference if you want written rationales |

Deeper references: [USER_GUIDE.md](USER_GUIDE.md) §9 and
[PLANNING_GUIDE.md](PLANNING_GUIDE.md) (edit API and audit actions),
[MIGRATABILITY_ASSESSMENT.md](MIGRATABILITY_ASSESSMENT.md) (why a VM is
blocked), [MTV-GROUNDING.md](MTV-GROUNDING.md) (how the YAML maps to MTV).
