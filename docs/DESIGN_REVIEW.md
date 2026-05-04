# Design Review — Network and Storage

The Design Review feature surfaces gaps between the source VMware
environment and a proposed OpenShift Virtualization design, running
entirely on the appliance's local LLM. Two review kinds share the same
shape and workflow:

| Kind | What it covers |
|------|----------------|
| **Network** | CUDN, NetworkAttachmentDefinition (NAD), NetworkPolicy, Multus config |
| **Storage** | StorageClass, VolumeSnapshotClass, StorageMap (Forklift), optional StorageProfile |

This is **decision support, not authoritative validation.** Every
finding is meant to land in front of a network or storage engineer for
review. The analyzer is honest about its limits — every finding carries
a confidence level so operators can prioritize high-confidence items
and triage the rest.

---

## How both reviews work

For each review you submit, VirtValidate aggregates three inputs and
runs a single LLM pass that produces structured findings:

| Input | Source |
|-------|--------|
| Source vSphere data | Pulled from `VM.vsphere_networks` (network) or `VM.vsphere_datastores` (storage). Populated by RVTools imports + baseline collection. |
| Customer notes | Plain-English markdown / text. Describes anything not visible in RVTools — zones, DMZ boundaries, latency requirements, replication topology, multipath policies. |
| Proposed OCP design | YAML manifests for the relevant subsystem. |

The LLM produces structured findings (category + severity + confidence
+ source evidence + proposed evidence + recommendation), and each
finding lands in its own DB row so operators can triage them as
**open**, **accepted**, or **dismissed** independently.

Status transitions: `draft → analyzing → completed` (or `failed`
on LLM error). State persists on the review row itself so polling
survives appliance restarts — no separate task store needed.

---

## Network review

### Categories

- **Coverage gaps** — VLANs / portgroups in source absent from proposed design.
- **Configuration mismatches** — MTU drift, VLAN tagging differences, IP-space conflicts, security-zone separation lost in translation.
- **Missing resources** — Customer mentions DMZ but no DMZ NetworkPolicy; multi-NIC source VMs but the proposed design only has a single NAD.
- **Positive confirmations** — Things the design correctly handles. Pure-negative reports erode trust.

### Worked example

**Customer notes:**

```
The DMZ must be air-gapped from internal networks. Storage segment uses
jumbo frames (MTU 9000). Application tier shares a portgroup with the
load balancers — that's intentional.
```

**Proposed YAML excerpt:**

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: api-frontend-nad
  namespace: finance-prod
spec:
  config: |
    {"cniVersion":"0.3.1","type":"bridge","mtu":1500}
```

**Sample findings produced:**

| Category | Severity | Title | Confidence |
|----------|----------|-------|------------|
| missing_resource | critical | No NetworkPolicy enforcing DMZ isolation | high |
| config_mismatch | medium | MTU drift between source and proposed network | medium |
| coverage_gap | high | DMZ portgroup absent from proposed CUDN set | high |
| positive_confirmation | info | Frontend portgroup correctly mapped | high |

---

## Storage review

### Categories

- **Performance tier mismatch** — VM was on SSD-backed datastore but mapped to HDD StorageClass. Customer notes specify `<5ms latency` but proposed StorageClass is shared general-purpose.
- **Capacity concern** — Source datastore at 80%+ utilization being consolidated to smaller proposed storage. Thin-provisioned VMs landing on a StorageClass that doesn't support thin (silent over-provisioning risk).
- **Access mode mismatch** — VM with shared disk on VMware (RWX intent — Oracle RAC, Microsoft cluster, GFS2) mapped to ReadWriteOnce StorageClass.
- **Replication loss** — Customer notes mention "replicated array" but proposed StorageClass has no replication. VM has disks across distinct datastores for HA but mapped to a single StorageClass.
- **Snapshot capability** — VMs rely on VMware snapshots; proposed StorageClass doesn't support CSI snapshots, or no VolumeSnapshotClass references the new SC.
- **Multipath policy** — Source has specific multipath policy (ALUA failover, round-robin, fixed); proposed storage doesn't specify or supports a different model.
- **Positive confirmations** — Performance tier alignment confirmed, capacity headroom adequate, replication preserved, snapshot capability maintained.

### Worked example

**Customer notes:**

```
Production databases need <5ms latency. Datastore 'prod-array-tier1' is
on a replicated array (synchronous replication to DR site). VMware
snapshots are used for daily backups. Multipath policy is round-robin
across two fabrics. RAC cluster shares VMDKs across 3 nodes on
prod-array-tier1.
```

**Proposed YAML excerpt:**

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: standard
provisioner: openshift-storage.rbd.csi.ceph.com
parameters:
  pool: rbd-pool
allowVolumeExpansion: true
reclaimPolicy: Delete
```

**Sample findings produced:**

| Category | Severity | Title | Confidence |
|----------|----------|-------|------------|
| replication_loss | critical | Replicated source array has no replication on target | high |
| performance_tier_mismatch | high | Production DB tier mapped to general-purpose StorageClass | high |
| access_mode_mismatch | high | Shared-disk RAC VMs mapped to ReadWriteOnce | medium |
| snapshot_capability | medium | VolumeSnapshotClass missing for proposed StorageClass | high |
| positive_confirmation | info | Capacity headroom adequate for consolidated storage | high |

### Storage-specific limitations

The analyzer is explicit about what it can and cannot do:

- **Cannot validate actual storage performance, only declared specs.** A StorageClass labeled "high-IOPS" might still throttle under load — the LLM evaluates intent, not benchmarks.
- **StorageClass capabilities depend on the backing CSI driver's behavior.** "Supports snapshots" is a CSI-driver assertion, not a guarantee that snapshots are crash-consistent or quiesced.
- **Snapshot quiescence requires application coordination not validated here.** A VolumeSnapshotClass produces a point-in-time copy of the volume; whether the database in that volume is consistent at the snapshot moment depends on application-level coordination (`fsfreeze`, DB hot-backup mode) that the appliance doesn't see.
- **Datastore tier (SSD vs HDD) is only known if the customer notes carry it.** The appliance sees datastore *names* in `VM.vsphere_datastores` — tier metadata, capacity utilization, and replication topology come from the operator's notes block. The LLM is encouraged to mark `low` confidence whenever notes are vague.

---

## How to use either review

### From the dashboard

1. Open the **Design Review** tab in the dashboard navigation.
2. Click **+ Network** or **+ Storage** depending on the layer you're reviewing.
3. Name the review (e.g. `Phase 1 cutover — DC-East prod tier`).
4. Upload or paste customer notes (markdown or plain text).
5. Upload or paste the proposed YAML manifests.
6. Check **Analyze immediately after creation** to fire the LLM right away (default).
7. Review findings on the detail page. Triage each as `open` / `accepted` / `dismissed`.
8. Re-run analysis after editing notes / YAML — old findings are replaced.

### Via the API

Endpoints mirror across kinds — replace `network-reviews` with `storage-reviews`:

```bash
# Create
curl -X POST http://<host>:8000/api/storage-reviews \
  -H "Content-Type: application/json" \
  -d '{"name": "phase-1-storage", "customer_notes": "...", "proposed_yaml": "..."}'

# Analyze (BackgroundTask — returns 202 immediately)
curl -X POST http://<host>:8000/api/storage-reviews/{id}/analyze

# Fetch findings
curl http://<host>:8000/api/storage-reviews/{id}

# Triage a finding
curl -X PATCH http://<host>:8000/api/storage-reviews/{id}/findings/{finding_id} \
  -H "Content-Type: application/json" \
  -d '{"triage": "accepted"}'
```

---

## Output schema

Both reviews emit the same shape — only the `category` enum differs.

```json
{
  "executive_summary": "one or two paragraphs summarizing overall design fit",
  "findings": [
    {
      "category": "<kind-specific category>",
      "severity": "critical | high | medium | low | info",
      "confidence": "high | medium | low",
      "title": "<short headline, <100 chars>",
      "description": "<plain-English explanation>",
      "source_evidence": "<quote or reference from source>",
      "proposed_evidence": "<quote or reference from proposed design, or '(absent)'>",
      "recommendation": "<actionable next step>"
    }
  ]
}
```

Categories per kind:

| Kind | Allowed categories |
|------|--------------------|
| Network | `coverage_gap`, `config_mismatch`, `missing_resource`, `positive_confirmation` |
| Storage | `performance_tier_mismatch`, `capacity_concern`, `access_mode_mismatch`, `replication_loss`, `snapshot_capability`, `multipath_policy`, `positive_confirmation` |

Severity / confidence / triage enums are shared across both review kinds.

---

## Honest limitations (apply to both kinds)

- **The LLM is reasoning, not authoritative.** Every finding needs a human sign-off before any action is taken.
- **The appliance only sees what RVTools captured + what the operator typed.** Vague notes produce low-confidence findings; that's working as intended.
- **The LLM may miss things.** Coverage isn't guaranteed — the analyzer identifies issues *it can find from the inputs*. A finding it didn't produce is not the same as "no issue exists."
- **Re-running analysis replaces findings.** Triage state is lost on re-run. If you need to preserve triage across runs, accept/dismiss findings before re-analyzing.

---

## Picking which kind for which review

Run **both** kinds for any non-trivial migration. They complement each other:

| Question | Kind |
|----------|------|
| Will east-west traffic break mid-cutover? | Network |
| Will my Oracle RAC cluster lose shared disks? | Storage |
| Did the operator forget a NetworkPolicy for the DMZ? | Network |
| Did the operator forget a VolumeSnapshotClass? | Storage |
| Will MTU drift cause TCP retransmits? | Network |
| Will my replicated array become a single point of failure? | Storage |

Network and storage reviews live in the same dashboard tab and share
audit-log conventions (`{kind}_review.create`,
`{kind}_review.analyze_triggered`, `{kind}_review.delete`). The
underlying tables (`network_design_reviews` / `storage_design_reviews`)
are independent — deleting one kind doesn't affect the other.
