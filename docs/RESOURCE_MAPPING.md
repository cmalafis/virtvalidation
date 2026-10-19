# Resource Mapping

A *resource mapping* is the bridge between one vCenter and one OpenShift
target cluster. It says, concretely:

- this vSphere port group → that target network (or: drop this NIC),
- this datastore → that StorageClass,
- VMs like these → that target namespace.

Plan generation (Stage 0) refuses to plan a VM whose networks, datastores or
namespace don't resolve through a mapping. The MTV YAML emitter (Stage 7)
reads the mapping *and the target cluster's catalog* to decide each
NetworkMap / StorageMap entry, so the YAML references real cluster resources
and the right MTV destination type.

VirtValidate never connects to the cluster. What the cluster offers is
declared in its **catalog** — by hand, or by pasting `oc get … -o yaml`
output into **Import from cluster**.

---

## 1. The target catalog

Each registered OpenShift target has three catalogs (OpenShift targets →
cluster → tabs) plus a **Platform support** tab.

### Networks (`TargetNetwork`)

| Field | Meaning |
|---|---|
| `name`, `network_type` | `nad` · `udn` · `cudn` · `pod` |
| `namespace` | Where a NAD / UDN lives. Blank for a CUDN (cluster-scoped; its NAD is rendered into every selected namespace). |
| `role` | `primary` (replaces the pod network for its namespace) or `secondary`. UDN/CUDN only. |
| `topology` | `layer2` · `layer3` · `localnet` · `bridge` · `sriov` · `other` |
| `vlan_id` | Access VLAN — the recommender matches vSphere port groups on it. |
| `physical_network_name` | Localnet only; must match an NNCP `ovn.bridge-mappings[].localnet`. |
| `subnets` | CIDRs. A primary UDN's subnets are checked against VMs that keep a static IP. |
| `ipam_mode` | `persistent` (keeps IPs across live migration — required for primary UDN VMs) · `disabled` (required for localnet VMs) · `none` |
| `binding` | `l2bridge` (default) · `passt` (Tech Preview) · `bridge` · `sriov` |
| `transport` | `geneve` · `evpn` (4.22+, primary CUDN, bare metal) |

### Storage classes (`TargetStorageClass`)

`access_mode`, `volume_mode` (`Block` / `Filesystem`, emitted as
`volumeMode`), `provisioner`, and MTV **storage copy offload**:
`copy_offload_vendor` (forklift `storageVendorProduct`) plus
`copy_offload_secret` (array credentials Secret in the MTV namespace). With
both set, the StorageMap entry carries `offloadPlugin.vsphereXcopyConfig`.

### Namespaces (`OCPTargetNamespace`)

`primary_udn_network` names the primary UDN/CUDN serving the namespace, if
any. The namespace label `k8s.ovn.org/primary-user-defined-network` is set at
creation and cannot be added later, so a VM mapped to a primary UDN must land
in a namespace declared as served by it.

### Import from cluster

`POST /api/ocp-targets/{id}/catalog/import` (`app.core.catalog_import`, pure
Python). The UI shows the command to run:

```bash
{ oc get network-attachment-definitions,userdefinednetworks -A -o yaml
  echo ---
  oc get clusteruserdefinednetworks,storageclasses,storageprofiles,nodenetworkconfigurationpolicies -o yaml
  echo ---
  oc get namespaces -l k8s.ovn.org/primary-user-defined-network -o yaml; } > cluster-catalog.yaml
```

Derivation: NAD `spec.config` CNI JSON (bridge / `ovn-k8s-cni-overlay` /
sriov); UDN / CUDN role, topology, subnets, IPAM, transport; primary-UDN
namespaces from the UDN's namespace, a CUDN's selector against pasted
Namespace objects, or the CUDN's `NetworkCreated` status condition;
StorageClass volume/access mode from the matching CDI StorageProfile (else a
provisioner heuristic); NNCP localnet bridge mappings. NADs owned by a UDN/CUDN
are skipped. `dry_run` (default) previews; applying creates and updates but
never deletes or clears a field the paste doesn't mention.

---

## 2. Anatomy of a mapping

```
ResourceMapping
├── vcenter_source_id / ocp_target_id      unique pair
├── network_mappings  [{source_network, target_network_name, target_network_type,
│                       target_namespace, ignored, confidence, rationale, method}]
├── storage_mappings  [{source_datastore, target_storage_class, access_mode,
│                       volume_mode, confidence, rationale, method}]
└── namespace_mappings  NamespaceStrategy {strategy: single | per_environment |
                        per_application, …}  (legacy: list of criteria rows)
```

`status` is recomputed on read: `complete` (every source in the vCenter scope
is mapped or ignored), `incomplete`, or `needs_review` (a row names something
no longer in the catalog).

---

## 3. What each row emits (Stage 7)

| Mapping row → catalog entry | NetworkMap destination |
|---|---|
| `ignored: true` | `{type: ignored}` |
| `pod` | `{type: pod}` |
| UDN / CUDN with `role: primary` | `{type: pod}` — MTV detects the primary UDN from the namespace and applies `l2bridge` |
| CUDN (secondary, e.g. localnet) | `{type: multus, name: <cudn>, namespace: <VM's namespace>}` |
| NAD / secondary UDN | `{type: multus, name, namespace: row → catalog → VM's namespace}` |
| catalog entry with no `role` | as before the catalog grew attributes: `multus` |

StorageMap: `storageClass`, `accessMode` and `volumeMode` (row override first,
then the catalog), and `offloadPlugin` when the StorageClass declares copy
offload. Every document is validated offline against the vendored forklift
v2.12.1 CRDs (`app.core.mtv_validate`) before it leaves the process.

---

## 4. Suggestions (rules first, LLM for the rest)

**Suggest mappings** (`POST /api/mappings/{id}/suggest-network|-storage`,
`app.core.mapping_suggester`) never auto-saves and never returns a 5xx:

1. **Rules.** A port group whose VLAN (RVTools `vPort` / `dvPort` sheets, or a
   number in its name) equals one catalog network's VLAN → `rule:vlan`, high.
   Same normalized name → `rule:name`, medium. A cluster with one
   StorageClass → `rule:single_target`.
2. **LLM** for the rest: ≤10 sources per call, each with a shortlist of ≤10
   candidates ranked by the Python scorer, plus what the target release
   supports (`platform_features.prompt_context`).
3. **Validate → retry (3×, with the error as feedback) → fall back** to the
   scorer's best candidate (`mechanical_fallback`, low). Networks are never
   guessed by the fallback — an unmatched port group stays unmapped. Auth and
   guardrail failures fall back too and raise the Settings banner.

Each row's `method` shows which path decided it. Every LLM call is captured
in the inference log (`operation: mapping_suggest_*`).

---

## 5. Platform checks (preflight and wave cards)

`app.core.mapping_checks` — deterministic and version-aware (judged against
the cluster's `ocp_version` / `mtv_version` through
`app.core.platform_features`). Shown in **Run preflight** and on each wave
card ("Where this wave lands"). Blocking findings make preflight `ok: false`;
plan generation is not refused on them.

| Code | Severity |
|---|---|
| `udn.static_ip_outside_subnet` — VM keeps a static IP outside the primary UDN's subnets | blocking |
| `udn.namespace_not_primary` — VM lands in a namespace not served by that primary UDN | blocking |
| `udn.primary_not_layer2`, `udn.ipam_not_persistent` | blocking |
| `localnet.bridge_mapping_missing`, `localnet.ipam_enabled` | blocking |
| `udn.evpn_unavailable` (EVPN before 4.22) | blocking |
| `offload.unavailable` (MTV < 2.10) | blocking |
| `udn.mtv_destination_support`, `udn.passt_tech_preview`, `udn.evpn_support_level` | warning |
| `storage.rwo_no_live_migration` | warning |
| `offload.vendor_developer_preview`, `offload.support_level`, `offload.no_secret`, `offload.nfs_datastore` | warning |
| `localnet.no_physical_network` | warning |
| `udn.many_networks` (> 72, MTV-5695), `target.version_unknown` | info |

Per-VM caveats that don't depend on the mapping (VM names over 47 characters
can't live-migrate, RHEL 9/10 static-IP known issue) are assessment rules —
see `MIGRATABILITY_ASSESSMENT.md`.

---

## Related docs

- `MTV-GROUNDING.md` — the sourced reference for every field emitted (§2.1, §12).
- `COMPATIBILITY.md` — OpenShift / MTV platform matrix.
- `TARGET_CLUSTERS.md` — registering clusters.
- `PLANNING_ARCHITECTURE.md` — Stages 0–7.
