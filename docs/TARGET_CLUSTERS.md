# OCP Target Clusters

A *target cluster* is the OpenShift Virtualization environment a
migration plan lands on. Registering targets is the prerequisite for
generating MTV YAML that references real cluster resources instead of
placeholder names.

This document covers registering targets, running discovery, and the
operational model around the discovery cache.

> Target registration is decoupled from migration execution. The
> appliance never *applies* YAML to the cluster — it only generates
> the manifests an operator pipes into `oc apply -f`. Discovery is a
> read-only contract.

---

## When to register a target

Register a cluster the first time you plan a migration to it. One
registration covers any number of plans / mappings / waves —
discovery results are cached on the row and refreshed on demand.

You'll typically have one or two targets per VirtValidate appliance:

- `ocp-prod-east` for the production cluster.
- `ocp-prod-west` if your deployment spans regions.
- `ocp-staging` for pre-cutover dry-runs.

A single appliance can sit between many vCenter sources and many
target clusters. The :class:`ResourceMapping` rows tie a specific
source/target pair together (see [`RESOURCE_MAPPING.md`](./RESOURCE_MAPPING.md)).

---

## Registering a target

### From the dashboard

1. Open **OCP Targets** in the top nav (or visit `/sources/targets`).
2. Click **+ Register Target**.
3. Fill in the form:
   - **Name** — friendly name, must be unique.
   - **API endpoint** — `https://api.<cluster>.<domain>:6443`.
   - **Region / Site** — informational, used for filtering in the UI.
   - **Classification** — UNCLASSIFIED / CUI / SECRET / TOP SECRET.
   - **Auth type** — bearer token, in-cluster service account, or kubeconfig.
   - **Verify TLS** — leave on for production; turn off only for dev
     clusters with self-signed certs.
4. Save. The new target lands in the list with status `NOT DISCOVERED`.

### Via the API

```bash
curl -X POST http://<host>:8000/api/sources/targets \
  -H "Content-Type: application/json" \
  -H "x-actor: $USER" \
  -d '{
    "name": "ocp-prod-east",
    "api_endpoint": "https://api.ocp-east.corp:6443",
    "region": "us-east",
    "site": "dc-iad",
    "classification_level": "cui",
    "auth_type": "token",
    "verify_ssl": true
  }'
```

---

## Running discovery

Discovery populates the target's cached resource lists:

| Cache field | Source | Used by |
|-------------|--------|---------|
| `storage_classes` | `GET /apis/storage.k8s.io/v1/storageclasses` | mapping editor (storage rows) |
| `network_attachments` | NAD + ClusterUserDefinedNetwork + UserDefinedNetwork | mapping editor (network rows) |
| `namespaces` | `GET /api/v1/namespaces` (system namespaces filtered) | mapping editor (namespace rows) |
| `cluster_capacity` | Node sums + KubeVirt VM count | wizard sanity hint |
| `mtv_namespace`, `ocp_version`, `kubernetes_version` | ClusterVersion + Forklift Provider | YAML generation defaults |

### Two discovery paths

**1. Live discovery** — fastest, but the appliance needs a route to the
target's API endpoint. Operator supplies a bearer token (`oc whoami -t`)
which is **not persisted** — VirtValidate uses it for the call and
discards it.

```bash
curl -X POST http://<host>:8000/api/sources/targets/$ID/discover \
  -H "Content-Type: application/json" \
  -d '{"bearer_token": "'"$(oc whoami -t)"'"}'
```

**2. Manual paste** — for air-gapped operators. Run `oc get -o json`
locally and paste the (lightly trimmed) output into the discovery
modal. The appliance never makes an outbound call.

```bash
# Storage classes — what the discovery endpoint expects
oc get storageclass -o json | jq '.items | map({
  name: .metadata.name,
  provisioner: .provisioner,
  is_default: (.metadata.annotations["storageclass.kubernetes.io/is-default-class"] == "true"),
  access_modes: [],
  reclaim_policy: .reclaimPolicy,
  volume_binding_mode: .volumeBindingMode
})'
```

The dashboard's **Discover** modal accepts paste-from-`jq` output for
all three resource types in dedicated text areas.

### Re-running discovery

Discovery is idempotent. Each run overwrites the cached fields and
stamps `last_synced_at`. Re-run when:

- the cluster's resource set changes (new StorageClass, NAD removed, etc.),
- a mapping shows status `needs_review` (drift detected),
- the appliance has been upgraded — schemas may have grown.

The discovery itself doesn't affect existing mappings; mapping
status is recomputed on read so changes surface without an explicit
refresh.

---

## Discovery failures

| Status code | Cause | Recovery |
|-------------|-------|----------|
| 401 / 403 | Token expired or insufficient RBAC | Get a fresh token; verify the token's user has `get` on storageclasses, NADs, namespaces, nodes, ClusterVersion |
| 404 on a CRD | OCP cluster has no NetworkAttachmentDefinition CRD installed | OK — `network_attachments` will be empty; install Multus / OVN if needed |
| 502 from VirtValidate | Network reachability issue or TLS verification failure | Use manual-paste discovery instead, or fix the route / cert chain |
| 502 with `Forklift` errors | Forklift / MTV not installed on cluster | Install MTV operator; `mtv_namespace` will be empty until then |

When discovery fails, the target row's `status` flips to `error` and
`last_error` carries the failure detail. The audit log records the
event:

```bash
curl 'http://<host>:8000/api/audit?action=ocp_target.discovery_failed' | jq
```

---

## RBAC: minimum permissions

VirtValidate's discovery requires a role the bearer token can assume.
A read-only ClusterRole works:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: virtvalidate-discovery
rules:
  - apiGroups: [""]
    resources: ["namespaces", "nodes"]
    verbs: ["get", "list"]
  - apiGroups: ["storage.k8s.io"]
    resources: ["storageclasses"]
    verbs: ["get", "list"]
  - apiGroups: ["k8s.cni.cncf.io"]
    resources: ["network-attachment-definitions"]
    verbs: ["get", "list"]
  - apiGroups: ["k8s.ovn.org"]
    resources: ["clusteruserdefinednetworks", "userdefinednetworks"]
    verbs: ["get", "list"]
  - apiGroups: ["config.openshift.io"]
    resources: ["clusterversions"]
    verbs: ["get", "list"]
  - apiGroups: ["forklift.konveyor.io"]
    resources: ["providers"]
    verbs: ["get", "list"]
  - apiGroups: ["kubevirt.io"]
    resources: ["virtualmachines"]
    verbs: ["get", "list"]
```

Bind it to a service account or to the user whose token you use.

---

## Classification levels

The `classification_level` field on a target carries through to:

- the dashboard list view (visual pill on each row),
- audit log entries (so federal reviewers can filter),
- mappings (a mapping inherits the target's classification level by reference).

Classification is **advisory** — the appliance doesn't enforce
authentication, network isolation, or marking. Operators in classified
environments must still apply the appropriate boundary controls
externally (FIPS mode, air-gapped deployment, separate appliance per
classification).

---

## What the appliance does *not* do

- Apply YAML to the cluster. MTV YAML output is the operator's hand-off.
- Monitor MTV migration progress. Once the operator runs `oc apply`
  the migration moves to the cluster's domain.
- Auto-create namespaces / StorageClasses / NADs. Discovery is read-only.
- Store credentials. Bearer tokens are used in-flight and not persisted;
  service-account discovery uses the in-cluster token mounted at the
  standard path.

---

## Related docs

- [`RESOURCE_MAPPING.md`](./RESOURCE_MAPPING.md) — the mapping editor that consumes the discovery cache.
- [`PLANNING_GUIDE.md`](./PLANNING_GUIDE.md) — how plans reference mappings.
- [`API.md`](./API.md) — full API reference for the targets and mappings endpoints.
