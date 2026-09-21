# MTV / OpenShift Virtualization API Grounding Reference

**Retrieved: 2026-09-20.** Every claim below was read from a live source on
that date — none is from model recall. Anything that could not be confirmed is
marked `UNVERIFIED`.

> **Hard rule.** If a field is not in this document with a source, it does
> not appear in VirtValidate-generated YAML. To add a field: confirm it in a
> source below, add the row, then use it.

## 0. Versions and sources

| Key | Value | Source |
|---|---|---|
| Latest MTV | **2.12** (z-streams through 2.12.8 in release notes) | [S-RN] |
| Upstream tag matching MTV 2.12 | `kubev2v/forklift` **`v2.12.1`** (published 2026-06-30) | GitHub releases API |
| MTV 2.12 ↔ OCP compatibility | The compat table is attribute-templated (`{ocp-y-version}`) in source; the rendered OCP version for 2.12 was **not retrieved** → `UNVERIFIED` that MTV 2.12 is the release paired with OCP 4.22 | [S-DOC] `ref_compatibility-guidelines.adoc` |
| VMware vSphere supported | 6.5 or later | [S-DOC] `ref_compatibility-guidelines.adoc` |
| OCP docs | 4.22 (`enterprise-4.22` branch of openshift-docs) | [S-OCP] |

Source keys used throughout:

| Key | URL |
|---|---|
| **[S-CRD]** | `https://raw.githubusercontent.com/kubev2v/forklift/v2.12.1/operator/config/crd/bases/forklift.konveyor.io_<plural>.yaml` (providers, networkmaps, storagemaps, plans, migrations, hooks) |
| **[S-CRD-main]** | same path on `main` @ `e0b911bb` (2026-09-19) — used only to flag post-2.12.1 fields |
| **[S-REGO]** | `https://github.com/kubev2v/forklift/tree/v2.12.1/validation/policies/io/konveyor/forklift/vmware` |
| **[S-SRC]** | `https://github.com/kubev2v/forklift/blob/v2.12.1/pkg/controller/plan/adapter/vsphere/builder.go` |
| **[S-DOC]** | `https://github.com/kubev2v/forklift-documentation` @ `3a11e610` (2026-09-15), `documentation/modules/<file>` — the AsciiDoc source of the Red Hat MTV guide |
| **[S-MIG]** | https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.12/html-single/migrating_your_virtual_machines_to_red_hat_openshift_virtualization/index |
| **[S-PLAN]** | https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.12/html-single/planning_your_migration_to_red_hat_openshift_virtualization/index |
| **[S-RN]** | https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.12/html-single/release_notes/index |
| **[S-OCP]** | `https://raw.githubusercontent.com/openshift/openshift-docs/enterprise-4.22/<path>` — source of https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/virtualization/networking |

Note on method: docs.redhat.com pages render client-side and could not be
read reliably by the fetcher, so the AsciiDoc sources those pages are built
from ([S-DOC], [S-OCP]) were read directly. [S-DOC] is the upstream doc repo
HEAD, not a 2.12-pinned branch; where it and [S-CRD] v2.12.1 disagree, the
CRD wins.

All six CRDs: group `forklift.konveyor.io`, version **`v1beta1`** (served +
storage), scope **Namespaced**. [S-CRD]

---

## 1. Provider  [S-CRD providers]

`spec` required: **`secret`, `type`**.

| Field | Type | Req | Notes |
|---|---|---|---|
| `type` | string | yes | No enum in the schema. `vsphere` used in [S-MIG] example. |
| `url` | string | no | vSphere: `https://<vCenter_host>/sdk` [S-MIG] |
| `secret` | ObjectReference (`name`, `namespace`, …) | yes | Secret keys `user`, `password`, `insecureSkipVerify`, `cacert`, `url` [S-MIG] |
| `settings` | object (free map) | no | `vddkInitImage`, `sdkEndpoint: vcenter\|esxi` [S-MIG] |

Destination provider for the local cluster is referenced as `name: host` in
the [S-MIG] example. **VirtValidate does not emit Provider CRs** (they carry
credentials); it references them by name/namespace only.

## 2. NetworkMap  [S-CRD networkmaps]

`spec` required: **`map`, `provider`**.

| Field | Type | Req | Enum / notes |
|---|---|---|---|
| `map[].source.id` | string | one of id/name | vSphere **network moRef** [S-DOC `proc_migrating-vms-cli-vmware.adoc`] |
| `map[].source.name` | string | one of id/name | "You can use either the `id` or the `name`" [ibid.] |
| `map[].source.namespace` / `.type` | string | no | |
| `map[].destination.type` | string | **yes** | **`pod` \| `multus` \| `ignored`** |
| `map[].destination.name` | string | no | NAD name when `multus` |
| `map[].destination.namespace` | string | no | "Required only when `type` is `multus`" [S-DOC ibid.] |
| `provider.source` / `provider.destination` | ObjectReference | **yes** | `name` + `namespace` |

Post-2.12.1 (on `main` only — **do not emit**): `map[].networkIPMode`
(`preserve|dhcp|none`), `map[].source.vlan`. [S-CRD-main]

### 2.1 Destination type ↔ OpenShift network construct

| What the operator has on the cluster | NetworkMap destination | Basis |
|---|---|---|
| Default pod network | `type: pod` | [S-DOC] |
| **Primary UDN / primary CUDN** (Layer2, replaces the pod network in a labelled namespace) | **`type: pod`** — there is no `udn` type. MTV detects that the *target namespace* has a primary UDN (`Plan.DestinationHasUdnNetwork`) and sets the `l2bridge` binding on the interface. | [S-SRC] builder.go L769-849; [S-DOC `con_about-udn.adoc`]: "MTV is now able to distinguish a UDN from a conventional pod network." Docs do not show YAML for it → the `type: pod` mapping is **source-derived**. |
| **Secondary localnet CUDN** | `type: multus`, `name: <CUDN name>`, `namespace: <VM's target namespace>` | The CUDN controller creates a NAD in every selected namespace ([S-OCP] `modules/cudn-status-conditions.adoc`: "NetworkAttachmentDefinition has been created in following namespaces"); the VM's `multus.networkName` is "the name of the localnet ClusterUserDefinedNetwork object" ([S-OCP] `modules/virt-attaching-vm-to-secondary-udn.adoc`). That the generated NAD's name equals the CUDN name is implied by that sentence; not stated as a rule → treat as `UNVERIFIED` until checked on a cluster with `oc get net-attach-def -n <ns>`. |
| Linux-bridge / SR-IOV NAD | `type: multus`, NAD name + NAD namespace | [S-DOC] |
| NIC to be dropped | `type: ignored` | [S-DOC] "Use `ignored` to avoid attaching VMs to this network" |

UDN-specific constraints: provider (vCenter) IP must be **outside the UDN
subnet or the migration fails** [S-DOC `proc_migrating-vms-cli-vmware.adoc`
L499]; MAC preservation on UDN depends on controller setting
`UdnSupportsMac`, static IPs on UDN on `StaticUdnIpAddresses` (feature flag)
[S-SRC], [S-RN]; known issue: >72 UDNs created simultaneously causes node
failures (MTV-5695) [S-RN].

## 3. StorageMap  [S-CRD storagemaps]

`spec` required: **`map`, `provider`**.

| Field | Type | Req | Enum / notes |
|---|---|---|---|
| `map[].source.id` / `.name` | string | one | vSphere **datastore moRef** or name |
| `map[].destination.storageClass` | string | **yes** | |
| `map[].destination.accessMode` | string | no | **`ReadWriteOnce` \| `ReadWriteMany` \| `ReadOnlyMany`** |
| `map[].destination.volumeMode` | string | no | **`Filesystem` \| `Block`** |
| `map[].offloadPlugin.vsphereXcopyConfig.secretRef` | string | yes (within) | storage copy offload |
| `map[].offloadPlugin.vsphereXcopyConfig.storageVendorProduct` | string | yes (within) | `flashsystem, vantara, ontap, primera3par, pureFlashArray, powerflex, powermax, powerstore, infinibox` |

When access/volume mode are omitted, the provisioner's StorageProfile decides
(e.g. Ceph RBD → Block/RWO; CephFS → Filesystem/RWX). **"ReadWriteOnce access
mode does not support live VM migration."** Without dynamic provisioning:
Filesystem + RWO. EXT4 on block: raise CDI filesystem overhead above 10%.
[S-PLAN]

Shared disks are a **Plan** concern (`migrateSharedDisks`), not a StorageMap
one. Post-2.12.1 only: `offloadPlugin.csiVolumeImport`. [S-CRD-main]

## 4. Plan  [S-CRD plans]

`spec` required: **`map`, `provider`, `targetNamespace`, `vms`**.

| Field | Type | Default | Notes |
|---|---|---|---|
| `targetNamespace` | string | — | required |
| `provider.source/destination` | ObjectReference | — | one source provider per Plan |
| `map.network` / `map.storage` | ObjectReference | — | |
| `type` | string | — | **`cold` \| `warm` \| `live` \| `conversion`**. "Supersedes the `warm` boolean if set." |
| `warm` | boolean | — | **Deprecated** ("will be deprecated in 2.10. Use Type instead"). |
| `description` | string | — | |
| `archived` | boolean | — | |
| `preserveStaticIPs` | boolean | **true** | vSphere. Needs vNIC properties reported by VMware Tools; VM must be powered on [S-REGO `ip_state`] |
| `preserveClusterCpuModel` | boolean | — | **oVirt only** ("the CPU model … in its oVirt cluster") — not meaningful for vSphere |
| `migrateSharedDisks` | boolean | **true** | |
| `rdmAsLun` | boolean | — | RDM disks attached as `lun: {}` |
| `skipGuestConversion` | boolean | false | raw-copy mode |
| `useCompatibilityMode` | boolean | true | only with `skipGuestConversion` |
| `runPreflightInspection` | boolean | true | warm VMware only |
| `deleteVmOnFailMigration` | boolean | true | |
| `deleteGuestConversionPod` | boolean | — | |
| `installLegacyDrivers` | boolean | auto | Win XP/2003/Vista/2008/7/2008R2 (no SHA-2) |
| `pvcNameTemplate`, `pvcNameTemplateUseGenerateName` (default true), `volumeNameTemplate`, `networkNameTemplate` | string/bool | — | Go templates |
| `targetPowerState` | string | auto | `on \| off \| auto` |
| `targetLabels`, `targetNodeSelector`, `targetAffinity` | object | — | |
| `convertorLabels`, `convertorNodeSelector`, `convertorAffinity` | object | — | |
| `transferNetwork` | ObjectReference (NAD) | — | |
| `conversionTempStorageClass` / `…Size` | string | — | |
| `customizationScripts`, `tagMapping`, `serviceAccount`, `virtV2vImage`, `xfsCompatibility`, `enableNestedVirtualization`, `skipZoneNodeSelector` | — | — | present in v2.12.1 |
| `diskBus` | string | — | Deprecated |

`vms[]` (no required keys in schema):

| Field | Notes |
|---|---|
| **`id`** | "The object ID. **vsphere: The managed object ID.**" e.g. `vm-1234` |
| **`name`** | "vsphere: A qualified name." [S-DOC]: "Use either the `id` or the `name`". |
| `targetName` | exact target VM name; migration fails if not unique |
| `hooks[]` | `{hook: ObjectReference, step: PreHook\|PostHook}` [S-MIG] |
| `luks` | ObjectReference to Secret of passphrases |
| `nbdeClevis` | boolean; Tang must be reachable from the target network |
| `rootDisk`, `instanceType`, `targetPowerState`, `migrateSharedDisks`, `rdmAsLun`, `deleteVmOnFailMigration`, `pvcNameTemplate`, `volumeNameTemplate`, `networkNameTemplate`, `enableNestedVirtualization` | per-VM overrides |

Post-2.12.1 only (**do not emit**): `scsiReservation`,
`selinuxRelabelAtBoot`, `selinuxRelabelExclude`, `vms[].excludeDisks`.
[S-CRD-main] (Release notes list some of these under 2.12.z; the v2.12.1 CRD
does not have them — a z-stream mismatch. Treat as `UNVERIFIED` per target
cluster.)

**VM identifier conclusion:** `vms[].id` is the vSphere **MoRef**. Putting a
display name in `id` is wrong. With no MoRef available, emit **`name` only**
and omit `id`.

## 5. Migration  [S-CRD migrations]

`spec` required: **`plan`** (ObjectReference). Optional: `cutover`
(date-time string, warm cutover), `cancel[]` (`id`/`name` VM refs).
Post-2.12.1 only: `resumeConversion`, `vmCutover[]`.

Creating a Migration **starts execution**. VirtValidate bundles should ship
it as a separate, clearly-named file that is not applied by the default
apply command.

## 6. Hook  [S-CRD hooks]

No required spec fields. `image`, `playbook` (base64 Ansible), `serviceAccount`,
`deadline` (int), `aap{url, jobTemplateId (req), tokenSecret, timeout}`.
Pre-migration hooks need VMware Tools on the source VM. [S-DOC `ref_vmware-prerequisites.adoc`]

---

## 7. Warm vs cold  [S-PLAN], [S-DOC `ref_vmware-prerequisites.adoc`]

| | Cold | Warm |
|---|---|---|
| Source VM | powered off during copy | runs during precopy; shut down at cutover |
| Requires | — | **CBT enabled on the VM and every disk**; **VMware Tools**; Windows: **VSS** + **VMware Snapshot Provider** services Manual/Automatic |
| Mechanism | one full copy, then virt-v2v | hourly (default) CBT snapshots; max **28 CBT snapshots** per VM |
| Providers | all | vSphere, RHV only |
| Don't | — | "Do not take a snapshot of a VM after you start a migration" |

## 8. Concurrency and plan sizing  [S-DOC]

- `controller_max_vm_inflight` — **default 20**. VMware cold→local: **VMs per
  ESXi host**; VMware warm (and cold→remote): **disks per ESXi host**.
  (`ref_max-concurrent-vms.adoc`, `ref_control-concurrent-disk-migrations.adoc`)
- ">10 VMs from an ESXi host in the same migration plan → must increase the
  host's NFC service memory" (`ref_vmware-prerequisites.adoc`).
- **Max 500 disks referenced by one plan** (MTV-1203); several ~100-VM plans
  run faster than one large plan (800 VMs / 8 plans: 57 min vs 500 VMs / 1
  plan: 5 h 10 min) (`ref_multiple-migration-plans-vs-single.adoc`).

Implication: the documented limits are **per ESXi host** and **per plan
(disks)** — not "10 VMs per wave". VirtValidate does not currently know a
VM's ESXi host or disk count.

## 9. Plan lifecycle  [S-MIG]

Cancel stops in-flight VMs only; a plan with failed VMs can be **restarted**
(failed VMs retried) or **duplicated**; **archive** is irreversible (deletes
logs/history); delete without archive leaves temporary resources.
`deleteVmOnFailMigration` (default true) removes the failed target VM.

## 10. What MTV itself flags on a VMware VM  [S-REGO]

The complete v2.12.1 VMware policy set. This is the authoritative
"concerns" list; most are derivable from an RVTools export.

| Policy | Category | Assessment (abridged) |
|---|---|---|
| `datastore` | **Critical** | Disk not located on a datastore — cannot be migrated |
| `passthrough_device` | **Critical** | SCSI/PCI passthrough — cannot be migrated until removed |
| `rdm_disk` | Warning | RDM not supported with VDDK transfer; only via copy-offload (XCOPY), else remove + reattach |
| `disk_mode` | Warning | **Independent disks** cannot be transferred with VDDK; change to Dependent (or XCOPY) |
| `shared_disk` | Warning | Shared disk needs special handling (two-plan strategy) |
| `changed_block_tracking` | Warning | CBT must be enabled for warm |
| `consolidation_needed` | Warning | Snapshots need consolidation; delays precopies |
| `snapshot` | Information | Migrated with current snapshot; snapshots not carried over |
| `tpm_enabled` | Warning | **TPM data will not be transferred** |
| `vm_os` | Warning | Guest OS unsupported. Supported: RHEL 7/8/9/10, Windows 10/11, Server 2016/2019/2022/2025 (by `guestId` regex or Tools guest name) |
| `name` | Warning | Not RFC 1123 → renamed automatically |
| `hostname` | Warning | Empty hostname → may be renamed |
| `ip_state` | Warning | No IPs reported → static IP preservation needs powered-on VM + VMware Tools |
| `cpu_memory_hotplug` | Warning | Hot-add not carried over |
| `cpu_affinity`, `host_affinity`, `numa_affinity` | Warning | Dropped; re-create post-migration |
| `sriov_device`, `usb_controller` | Warning | Not migrated |
| `fault_tolerance`, `drs_enabled`, `dpm_enabled` | Information | Feature absent on target |
| `disk_serial_numbers` | Information | `disk.EnableUUID` serials truncated |

Further documented limitations:

- **NVMe disks: not supported.** [S-DOC `proc_migrating-vms-cli-vmware.adoc`]
- **Hibernated VMs: not supported**; disable hibernation. [S-DOC]
- **vSphere 6 / 7 VMs cannot be migrated to a FIPS-compliant OpenShift
  Virtualization cluster.** [S-DOC `ref_vmware-prerequisites.adoc`] — directly
  relevant to federal targets.
- **vSAN-backed VMs require a VDDK image.** [S-DOC]
- Target namespace needs egress to vSphere; NetworkPolicies blocking it fail
  the migration. [S-DOC]
- **Windows Measured Boot: cannot migrate.** **Secure Boot: may fail to boot**
  on destination (MTV-1548). [S-DOC `ref_source-vm-migration-considerations.adoc`]
- Dual-boot: first boot disk found is converted (or set `rootDisk`). [S-DOC]
- **Windows Server 2012 R2 VMs don't boot** — no virtio drivers in the current
  package (known issue). [S-RN]
- **RHEL 9/10 predictable NIC naming overrides udev rules → static IP loss**
  (known issue). CentOS 7.9 from vSphere 7: NIC names change, static IP
  breaks. [S-RN], [S-DOC]
- Encryption: LUKS (passphrase Secret **or** NBDE/Clevis — mutually exclusive)
  and BitLocker supported. [S-DOC `ref_encryption-support.adoc`, `con_migration-of-luks-encrypted-disks.adoc`]
- `UNVERIFIED`: VM hardware-version floor, Windows dynamic disks, btrfs root —
  no statement found in MTV 2.12 sources (they are governed by the virt-v2v
  support article https://access.redhat.com/articles/1351473, not retrieved).

## 11. Guest changes made by MTV (virt-v2v)  [S-DOC `con_virt-v2v-mtv-main-functions.adoc`]

Adds: **VirtIO drivers** (network, disk), **QEMU guest agent install**
(cold: at first boot — guest package manager must be able to install it
[S-PLAN]), bootloader/boot-entry updates. Removes: **VMware Tools**, VMware
NIC driver config, incompatible boot settings. Sets IP addresses during
migration or on first reboot (VMware, OVA). MAC addresses are preserved
[S-PLAN]. RAM state is never migrated.

Not preserved: TPM contents, snapshots, hot-add, CPU/host/NUMA affinity,
DRS/DPM/FT, USB/SR-IOV/passthrough devices, NIC names (guest-dependent).

## 12. OCP 4.22 VM networking  [S-OCP]

- CRDs: namespace-scoped **`UserDefinedNetwork`** and cluster-scoped
  **`ClusterUserDefinedNetwork`**, `apiVersion: k8s.ovn.org/v1`.
  (`virt/vm_networking/virt-connecting-vm-to-primary-udn.adoc`)
- **Primary UDN** replaces the pod network for the namespace. Namespace must
  carry label **`k8s.ovn.org/primary-user-defined-network`**, applied **at
  namespace creation**; not `default`/`openshift-*`. For VMs: `topology:
  Layer2`, `role: Primary`, and **`ipam.lifecycle: Persistent`** (with
  `subnets`) "to ensure VM live migration support".
  (`modules/virt-creating-a-primary-udn.adoc`)
- VM attaches via `networks: [{name, pod: {}}]` + interface
  `binding.name: l2bridge` (default) or `passt` (Technology Preview).
  (`modules/virt-attaching-vm-to-primary-udn.adoc`)
- Primary-UDN limitations: no `virtctl ssh`, no `oc port-forward`, no headless
  services. General UDN: namespace + network must exist **before** workloads;
  UDN/CUDN **cannot be modified after creation**; no default-network services
  (e.g. image registry). (`modules/nw-udn-limitations.adoc`)
- **Secondary localnet CUDN**: `topology: Localnet`, `role: Secondary`
  (required), `physicalNetworkName` must match an NMState NNCP
  `ovn.bridge-mappings[].localnet`; **`ipam.mode: Disabled`** required
  ("does not support configuring IPAM for virtual machines"); VM uses
  `multus.networkName: <cudn name>` with `bridge: {}`. MultiNetworkPolicy:
  `ipBlock` peers only. Linux bridge bonding modes 0, 5, 6 unsupported.
  (`modules/virt-creating-secondary-localnet-udn.adoc`,
  `modules/virt-attaching-vm-to-secondary-udn.adoc`)
- BGP EVPN for primary CUDNs is available in 4.22. (assembly note)

---

## 13. Current generator vs this reference (`backend/app/core/mtv.py`)

| Line | Today | Grounded verdict |
|---|---|---|
| `:337` | `vms[].id` = vSphere **display name** | **Wrong.** `id` is the MoRef. Emit `name` only until MoRef is ingested. |
| `:337` | `vms[].name` = RFC1123-slugified name | **Wrong.** `name` is the *source* qualified name; the slug belongs in `targetName` (or omit — MTV renames automatically). |
| `:355` | `warm: True` hardcoded | Deprecated field; and forces CBT/Tools prerequisites on every VM. Emit `type: cold\|warm` from an operator choice. |
| `:295` | StorageMap emits `storageClass` only | `accessMode`/`volumeMode` are valid and the operator's choice is dropped. |
| `:237-255` | unmapped network → silent `type: pod` | Valid YAML, wrong migration. Should be an error or explicit `ignored`. |
| — | `cudn`/`udn` catalog types all emit `multus` | Primary UDN must emit `type: pod`; only secondary CUDN/NAD emit `multus`. |
| — | `preserveStaticIPs` not set | Defaults true; fine, but should be explicit + surfaced. |
| — | apiVersion `forklift.konveyor.io/v1beta1`, three kinds, provider refs by name | Correct. |
