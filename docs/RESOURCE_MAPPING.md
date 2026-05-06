# Resource Mapping

A *resource mapping* is the bridge between a vCenter source and an
OCP target cluster. It says, concretely:

- this source vSphere portgroup → that target NetworkAttachmentDefinition,
- this source datastore → that target StorageClass,
- VMs matching this criterion → that target namespace.

The MTV YAML generator pulls names from the mapping at export time so
the YAML references real cluster resources, not placeholders.

> Without a mapping, the YAML generator falls back to per-VM
> `target_*` columns. Those columns were a v0.1.x compromise — they
> don't know which cluster you're targeting and routinely produce
> YAML the cluster rejects. **Always create a mapping before
> generating MTV YAML for production use.**

---

## Anatomy of a mapping

```
ResourceMapping
├── name                    Phase 1 cutover
├── vcenter_source_id   →   VCenterSource (left side)
├── ocp_target_id       →   OCPTarget (right side)
├── status                  complete | incomplete | needs_review
├── is_active               True for the default one in this pair
├── network_mappings []     [{source_network, target_network_name, target_namespace, ...}, ...]
├── storage_mappings []     [{source_datastore, target_storage_class, access_mode, ...}, ...]
└── namespace_mappings []   [{criteria, criteria_value, target_namespace}, ...]
```

`network_mappings` and `storage_mappings` are **lookup tables** — one
entry per source resource. `namespace_mappings` is **rule-based** —
walked in order, first match wins, with a `default` fallback.

### Status

`complete` — every source network and datastore in the vCenter scope has a target,
and every target referenced is still present in the cluster's discovery cache.

`incomplete` — at least one source resource has no target. MTV YAML
will skip those entries (and likely fail to apply on the cluster).

`needs_review` — drift detected: the mapping references a target
resource that was present at mapping-time but is no longer in the
cluster's discovery cache. Re-run discovery, then fix the mapping.

Status is recomputed on every read so drift surfaces without an
explicit refresh.

---

## Creating a mapping

### From the dashboard

1. Visit **Mappings** in the top nav (or `/mappings`).
2. Click **+ New Mapping**.
3. Pick a source vCenter + a target OCP cluster + a friendly name.
4. (Optional) Mark the mapping active — this deactivates any other
   mapping for the same source/target pair. Active mappings are the
   ones the planner picks up by default if the wizard doesn't
   specify a `mapping_id`.
5. Save → you land on the editor.

### Via the API

```bash
curl -X POST http://<host>:8000/api/mappings \
  -H "Content-Type: application/json" \
  -H "x-actor: $USER" \
  -d '{
    "name": "phase-1",
    "vcenter_source_id": 1,
    "ocp_target_id": 1,
    "network_mappings": [],
    "storage_mappings": [],
    "namespace_mappings": [],
    "is_active": true
  }'
```

The response includes `status: incomplete` until you populate the
mapping rows.

---

## Editing the mapping

The editor renders the union of:

- **source signals** — every distinct network and datastore referenced
  by VMs in the source vCenter, with a VM count beside each row,
- **existing mapping rows** — what you've already set, including
  rows for sources no longer in the inventory (which you can delete
  by clearing the target).

For each source row, pick a target from the dropdown. The dropdown
options come from the OCP target's discovery cache.

### Suggesting mappings via the LLM

Click **🤖 Suggest** above the network table or the storage table to
ask the local LLM for matches. The LLM proposes one target per source
with a confidence score (`high` / `medium` / `low`) and a one-line
rationale.

The suggestion engine never auto-commits — every row is reviewed.
The LLM is also constrained to pick from the discovered target set;
hallucinated names are dropped before the suggestions reach the UI,
and the rationale_summary notes how many drops happened.

The model and prompts are documented in [`LLM_PROMPTS.md`](./LLM_PROMPTS.md).

### Pre-flight check

Click **✓ Preflight** at the top of the editor to validate the
mapping against the inventory and the cluster:

| Check | Failure means |
|-------|---------------|
| `unmapped_networks` | Source networks the inventory references that have no target row |
| `unmapped_datastores` | Same for storage |
| `missing_storage_classes_on_target` | Mapping references a StorageClass the cluster doesn't have |
| `missing_networks_on_target` | Same for NADs / CUDNs |
| `warnings` | Soft signals — no namespace mapping configured, target is inactive, etc. |

The check is non-destructive. The dashboard surfaces the result in a
banner above the editor.

---

## How plan generation uses the mapping

When the wizard submits, an optional `mapping_id` accompanies the
plan:

```json
POST /api/plans/generate
{
  "name": "Q3 cutover plan",
  "inline_strategy": { ... },
  "scope": {"source_vcenter_id": 1},
  "mapping_id": 7
}
```

Pre-flight on the trigger endpoint:

- `404` if `mapping_id` doesn't exist,
- `409` if the mapping's `vcenter_source_id` doesn't match the scope's
  `source_vcenter_id` (you're targeting a vCenter the mapping doesn't cover).

The plan stores `mapping_id` so re-generating MTV YAML weeks later
uses the same mapping the operator chose at planning time, even if the
mapping has been edited or deactivated since.

The plan also records mapping-driven warnings (`incomplete`,
`needs_review`, or "no mapping" hint) on its `warnings` array so the
plan view surfaces them.

---

## How MTV YAML export uses the mapping

`GET /api/plans/{plan_id}/waves/{wave_number}/mtv-yaml` resolves
references this way:

| YAML field | Source |
|------------|--------|
| `NetworkMap.spec.map[].destination.name` / `namespace` / `type` | mapping → `network_mappings` |
| `StorageMap.spec.map[].destination.storageClass` | mapping → `storage_mappings` |
| `Plan.spec.targetNamespace` and per-VM overrides | mapping → `namespace_mappings` |

If the mapping is missing or doesn't cover a source resource, the
generator falls back to per-VM `target_*` fields. If those are also
empty:

- networks fall back to `pod` (so the YAML stays valid),
- storage rows are skipped (operators see an `MTVGenerationError`
  with a clear "set vsphere_datastores and target_storage_class"
  message).

If the plan references a `mapping_id` that's been deleted, the YAML
endpoint returns `409` with a clear "re-generate the plan" message
rather than silently producing wrong YAML.

---

## Single-active mapping rule

Per (source, target) pair, only one mapping can be `is_active`. When
you flag a mapping active, the API automatically deactivates the
others — so you can keep mappings around as drafts and promote one at
a time.

Plan generation doesn't *enforce* `is_active` — `mapping_id` is the
explicit signal. Active is a UI/operator convenience for "this is the
default one I'd grab."

---

## Worked example: phase-1 cutover

```yaml
ResourceMapping: Phase 1 cutover
  vCenter: vc-east-prod (40 VMs)
  Target:  ocp-east-prod
  Status:  complete · active

  Network mappings (3 rows):
    web-vlan-100         → prod-vlan-100 (NAD, openshift-multus)        [confirmed]
    db-vlan-110          → prod-vlan-110 (NAD, openshift-multus)        [confirmed]
    dmz-vlan-200         → dmz-vlan-200  (NAD, openshift-multus)        [LLM-suggested high]

  Storage mappings (2 rows):
    array-tier1-ssd      → ocs-storagecluster-ceph-rbd                  [confirmed]
    array-bulk-hdd       → ocs-storagecluster-cephfs (RWM)              [confirmed]

  Namespace mappings (2 rows):
    application=epic-emr → epic-emr-prod
    default              → finance-prod
```

When the planner generates `Q3 cutover` against this mapping, every
generated MTV `Plan`/`NetworkMap`/`StorageMap` references the
right-hand side names verbatim — no operator needs to edit the YAML
before applying.

---

## Related docs

- [`TARGET_CLUSTERS.md`](./TARGET_CLUSTERS.md) — registering and discovering targets.
- [`PLANNING_GUIDE.md`](./PLANNING_GUIDE.md) — how plans reference mappings.
- [`LLM_PROMPTS.md`](./LLM_PROMPTS.md) — what the suggestion LLM is told.
