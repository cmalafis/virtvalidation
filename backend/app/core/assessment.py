"""Migratability assessment — can this VM migrate, and what will it lose?

Deterministic. No LLM anywhere in this module: a verdict that ends up in a
change record has to trace to a rule, not to a model (CLAUDE.md, "LLM Input
Discipline").

The rules mirror the VMware validation policies MTV itself runs
(``kubev2v/forklift`` v2.12.1, ``validation/policies/io/konveyor/forklift/
vmware`` — see docs/MTV-GROUNDING.md §10) and reuse MTV's concern **ids**
and categories, so a finding raised here in week 1 is the same finding the
operator sees in the MTV console on migration day, under the same name.
Rules with a ``vv.`` prefix are VirtValidate's own, each derived from a
documented MTV limitation that has no forklift policy.

The input is what an RVTools export can say. Where it can't say enough, the
rule is reported under ``not_evaluated`` with the reason — an assessment
that silently skips a check it couldn't run reads as a clean bill of
health, which is the failure this module exists to prevent.

Status roll-up:

    blocked   any Critical finding — MTV will refuse this VM as-is
    warning   any Warning finding — migrates, but something is lost or
              needs work before/after
    ok        nothing above Information
    unknown   no RVTools facts on this VM (added by hand)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

CRITICAL = "Critical"
WARNING = "Warning"
INFORMATION = "Information"

RULESET_VERSION = "forklift-2.12.1+vv1"

# forklift `name.rego`: DNS-subdomain-ish, under 64 chars.
_VALID_NAME_RE = re.compile(r"^(([A-Za-z0-9][-A-Za-z0-9.]*)?[A-Za-z0-9])?$")

# forklift `vm_os.rego` — substrings of the guest OS full name.
_SUPPORTED_OS_SUBSTRINGS = (
    "red hat enterprise linux 7",
    "red hat enterprise linux 8",
    "red hat enterprise linux 9",
    "red hat enterprise linux 10",
    "windows 10",
    "windows 11",
    "windows server 2016",
    "windows server 2019",
    "windows server 2022",
    "windows server 2025",
)

_INDEPENDENT_MODES = ("independent_persistent", "independent_nonpersistent")


@dataclass(frozen=True)
class Finding:
    id: str
    category: str
    label: str
    assessment: str
    remediation: str
    evidence: dict[str, Any] = field(default_factory=dict)
    # "warm" when the finding only matters for warm migration.
    applies_to: str = "all"


@dataclass(frozen=True)
class Skipped:
    id: str
    label: str
    reason: str


@dataclass
class Assessment:
    status: str
    findings: list[Finding]
    not_evaluated: list[Skipped]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ruleset": RULESET_VERSION,
            "status": self.status,
            "findings": [asdict(f) for f in self.findings],
            "not_evaluated": [asdict(s) for s in self.not_evaluated],
        }


@dataclass(frozen=True)
class VMFacts:
    """The slice of a VM the rules read. Built from the ORM row by
    :func:`facts_from_vm`; tests build it directly."""

    name: str
    source_hostname: str | None = None
    ip_address: str | None = None
    power_state: str | None = None
    guest_os_full: str | None = None
    vsphere_datastores: tuple[str, ...] = ()
    disk_count: int | None = None
    hardware: dict[str, Any] = field(default_factory=dict)

    @property
    def disks(self) -> list[dict[str, Any]]:
        return list(self.hardware.get("disks") or [])

    @property
    def has_disk_detail(self) -> bool:
        return bool(self.disks)

    @property
    def powered_on(self) -> bool:
        return (self.power_state or "").lower() == "poweredon"


def facts_from_vm(vm: Any) -> VMFacts:
    return VMFacts(
        name=vm.name or "",
        source_hostname=vm.source_hostname,
        ip_address=vm.ip_address,
        power_state=vm.power_state,
        guest_os_full=vm.guest_os_full,
        vsphere_datastores=tuple(vm.vsphere_datastores or ()),
        disk_count=vm.disk_count,
        hardware=dict(vm.hardware_facts or {}),
    )


# --------------------------------------------------------------------------
# Rules. Each returns a Finding, a Skipped, or None (evaluated, clean).
# --------------------------------------------------------------------------
Rule = Callable[[VMFacts], "Finding | Skipped | None"]
_RULES: list[Rule] = []


def _rule(fn: Rule) -> Rule:
    _RULES.append(fn)
    return fn


def _names(disks: list[dict]) -> list[str]:
    return [d.get("label") or "?" for d in disks]


def _needs_vdisk(rule_id: str, label: str) -> Skipped:
    return Skipped(
        rule_id,
        label,
        "the import had no vDisk rows for this VM — re-export from RVTools with all sheets",
    )


@_rule
def datastore_missing(f: VMFacts):
    label = "Disk is not located on a datastore"
    orphans = [d for d in f.disks if not d.get("datastore")]
    if orphans or (not f.has_disk_detail and not f.vsphere_datastores):
        return Finding(
            "vmware.datastore.missing",
            CRITICAL,
            label,
            "The VM is configured with a disk that is not located on a datastore. "
            "The VM cannot be migrated.",
            "Move the disk onto a datastore (Storage vMotion), or confirm the RVTools "
            "export is complete — a VM row with no datastore at all usually means a "
            "truncated export.",
            {"disks": _names(orphans)} if orphans else {"datastores": []},
        )
    return None


@_rule
def rdm_disk(f: VMFacts):
    label = "Raw Device Mapped disk detected"
    if not f.has_disk_detail:
        return _needs_vdisk("vmware.disk.rdm.detected", label)
    rdm = [d for d in f.disks if d.get("raw")]
    if rdm:
        return Finding(
            "vmware.disk.rdm.detected",
            WARNING,
            label,
            "RDM disks are not supported when using VDDK transfer. If copy-offload "
            "(XCOPY) is enabled in the migration plan, RDM disks can be migrated. "
            "Otherwise, the RDM disks must be removed before migration and reattached after.",
            "Decide per LUN: convert the RDM to a VMDK before migration, use storage "
            "copy offload if your array is supported, or detach it and present the LUN "
            "to the migrated VM (Plan `rdmAsLun`).",
            {
                "disks": _names(rdm),
                "compat_modes": sorted({d.get("raw_compat_mode") or "?" for d in rdm}),
            },
        )
    return None


@_rule
def independent_disk(f: VMFacts):
    label = "Independent disk detected"
    if not f.has_disk_detail:
        return _needs_vdisk("vmware.disk_mode.independent", label)
    hit = [d for d in f.disks if (d.get("mode") or "").lower() in _INDEPENDENT_MODES]
    if hit:
        return Finding(
            "vmware.disk_mode.independent",
            WARNING,
            label,
            "Independent disks cannot be transferred using VDDK. If copy-offload (XCOPY) "
            "is enabled in the migration plan, independent disks can be migrated. Otherwise, "
            "the disks must be changed to 'Dependent' mode in VMware before migration.",
            "In vSphere, power the VM off and change the disk mode to Dependent. "
            "Independent disks are excluded from snapshots, which is how MTV reads them.",
            {"disks": _names(hit)},
        )
    return None


@_rule
def shared_disk(f: VMFacts):
    label = "Shared disk detected"
    if not f.has_disk_detail:
        return _needs_vdisk("vmware.disk.shared.detected", label)
    hit = [
        d
        for d in f.disks
        if (d.get("sharing") or "sharingNone") != "sharingNone"
        or (d.get("shared_bus") or "noSharing") != "noSharing"
    ]
    if hit:
        return Finding(
            "vmware.disk.shared.detected",
            WARNING,
            label,
            "The VM has a disk that is shared with another VM. Shared disks require "
            "special handling during migration.",
            "Migrate the cluster nodes together using MTV's two-plan approach: one plan "
            "with `migrateSharedDisks: true` that moves the shared disk once, and a second "
            "with `migrateSharedDisks: false` for the remaining nodes. Do not spread these "
            "VMs across waves.",
            {"disks": _names(hit)},
        )
    return None


@_rule
def nvme_disk(f: VMFacts):
    label = "NVMe disk detected"
    if not f.has_disk_detail:
        return _needs_vdisk("vv.disk.nvme.detected", label)
    hit = [d for d in f.disks if "nvme" in (d.get("controller") or "").lower()]
    if hit:
        return Finding(
            "vv.disk.nvme.detected",
            CRITICAL,
            label,
            "MTV does not support migrating VMware Non-Volatile Memory Express (NVMe) disks.",
            "Move the disk to a SCSI or SATA controller in vSphere before migration.",
            {"disks": _names(hit)},
        )
    return None


@_rule
def changed_block_tracking(f: VMFacts):
    label = "Changed Block Tracking (CBT) not enabled"
    cbt = f.hardware.get("cbt")
    if cbt is None:
        return Skipped(
            "vmware.changed_block_tracking.disabled", label, "the export has no CBT column"
        )
    if cbt is False:
        return Finding(
            "vmware.changed_block_tracking.disabled",
            WARNING,
            label,
            "For VM warm migration, Changed Block Tracking (CBT) must be enabled in VMware.",
            "Enable CBT (`ctkEnabled` on the VM and `scsiX:Y.ctkEnabled` on each disk, then "
            "a stun/unstun cycle such as a snapshot create+delete), or migrate this VM cold.",
            applies_to="warm",
        )
    return None


@_rule
def consolidation_needed(f: VMFacts):
    if f.hardware.get("consolidation_needed"):
        return Finding(
            "vmware.consolidation_needed",
            WARNING,
            "Snapshot consolidation required",
            "VM has snapshots that require consolidation. This may cause delays between precopies.",
            "Run 'Consolidate' on the VM in vSphere before migration.",
        )
    return None


@_rule
def snapshot(f: VMFacts):
    snaps = f.hardware.get("snapshots") or []
    if snaps:
        return Finding(
            "vmware.snapshot.detected",
            INFORMATION,
            "VM snapshot detected",
            "Online snapshots are not currently supported by OpenShift Virtualization. "
            "VM will be migrated with current snapshot.",
            "Snapshots do not carry over — only the current state migrates. Delete the "
            "ones you don't need (they also slow the transfer) and keep a backup if any "
            "is a rollback point you rely on.",
            {"count": len(snaps), "names": [s.get("name") for s in snaps][:5]},
        )
    return None


@_rule
def guest_os(f: VMFacts):
    label = "Unsupported operating system detected"
    os_name = f.guest_os_full or f.hardware.get("guest_os_tools")
    if not os_name:
        return Skipped("vmware.os.unsupported", label, "the export has no guest OS for this VM")
    lower = os_name.lower()
    if not any(s in lower for s in _SUPPORTED_OS_SUBSTRINGS):
        return Finding(
            "vmware.os.unsupported",
            WARNING,
            label,
            "The guest operating system is not currently supported by the Migration "
            "Toolkit for Virtualization",
            "MTV converts guests with virt-v2v, which supports RHEL 7-10, Windows 10/11 and "
            "Windows Server 2016-2025. Other guests may still convert but are untested: "
            "trial-migrate a clone first, or plan a rebuild on OpenShift Virtualization.",
            {"guest_os": os_name},
        )
    return None


@_rule
def windows_2012_no_virtio(f: VMFacts):
    os_name = (f.guest_os_full or "").lower()
    if "windows server 2012" in os_name:
        return Finding(
            "vv.os.windows2012.no_virtio",
            WARNING,
            "Windows Server 2012 / 2012 R2 will not boot after migration",
            "Known issue in MTV 2.12: Windows Server 2012 R2 VMs do not boot because the "
            "current virtio-win package has no drivers for them.",
            "Upgrade the guest in place before migrating, or rebuild it on a supported "
            "Windows Server release.",
            {"guest_os": f.guest_os_full},
        )
    return None


@_rule
def vm_name(f: VMFacts):
    if not (_VALID_NAME_RE.match(f.name) and len(f.name) < 64):
        return Finding(
            "vmware.vm.name.invalid",
            WARNING,
            "Invalid VM Name",
            "The VM name does not comply with the DNS subdomain name format. Edit the name "
            "or it will be renamed automatically during the migration to meet RFC 1123.",
            "Nothing is required — MTV renames it. If anything keys on the VM name "
            "(monitoring, backup jobs, runbooks), choose the target name yourself with "
            "`targetName` in the Plan so the rename isn't a surprise.",
            {"name": f.name, "length": len(f.name)},
        )
    return None


@_rule
def hostname(f: VMFacts):
    # source_hostname defaults to the VM name when the export had no DNS
    # name, so "equals the VM name and the VM is off" means "not reported".
    host = (f.source_hostname or "").strip()
    if host == "localhost.localdomain":
        return Finding(
            "vmware.hostname.default",
            WARNING,
            "Default Host Name",
            "The 'hostname' is set to 'localhost.localdomain', which is a default value. "
            "The hostname might be renamed during migration.",
            "Set a real hostname in the guest before migration.",
        )
    if not f.powered_on and (not host or host == f.name):
        return Finding(
            "vmware.hostname.empty",
            WARNING,
            "Empty Host Name",
            "The 'hostname' field is missing or empty. The hostname might be renamed during migration.",
            "vSphere only learns the hostname from VMware Tools on a running guest. Power "
            "the VM on before the final export, or accept that MTV may rename it.",
        )
    return None


@_rule
def missing_ip(f: VMFacts):
    if not f.ip_address:
        return Finding(
            "vmware.vm_missing_ip.detected",
            WARNING,
            "VM is missing IP addresses",
            "Static IP preservation requires the VM to be powered on and running VMware tools.",
            "MTV preserves static IPs from what VMware Tools reports. Power the VM on with "
            "Tools running before migration, or plan to reconfigure its network afterwards.",
            {"power_state": f.power_state},
        )
    return None


@_rule
def hotplug(f: VMFacts):
    label = "CPU/Memory hotplug detected"
    cpu, mem = f.hardware.get("cpu_hot_add"), f.hardware.get("mem_hot_add")
    if cpu is None and mem is None:
        return Skipped(
            "vmware.cpu_memory.hotplug.enabled", label, "the import had no vCPU / vMemory rows"
        )
    if cpu or mem:
        return Finding(
            "vmware.cpu_memory.hotplug.enabled",
            WARNING,
            label,
            "Hot pluggable CPU or memory is not currently supported by Migration Toolkit "
            "for Virtualization. You can reconfigure CPU or memory after migration.",
            "The setting is dropped. If the workload relies on scaling up without a "
            "reboot, size the migrated VM for its peak, or enable CPU/memory hotplug on "
            "the OpenShift Virtualization VM afterwards.",
            {"cpu_hot_add": bool(cpu), "memory_hot_add": bool(mem)},
        )
    return None


@_rule
def fault_tolerance(f: VMFacts):
    state = (f.hardware.get("ft_state") or "").lower()
    if state and state not in ("notconfigured", "disabled"):
        return Finding(
            "vmware.fault_tolerance.enabled",
            INFORMATION,
            "Fault tolerance",
            "Fault tolerance is not currently supported by OpenShift Virtualization. The VM "
            "can be migrated but it will not have this feature in the target environment.",
            "There is no lock-step equivalent. Cover the availability requirement at the "
            "application layer, or with VM high availability (node-failure restart).",
            {"ft_state": f.hardware.get("ft_state")},
        )
    return None


@_rule
def cluster_rules(f: VMFacts):
    rules = f.hardware.get("cluster_rules")
    if rules:
        return Finding(
            "vmware.host_affinity.detected",
            WARNING,
            "DRS affinity / anti-affinity rule detected",
            "The VM will be migrated without node affinity, but administrators can set it "
            "after migration.",
            "Re-create the intent on the target: `targetAffinity` in the MTV Plan, or "
            "pod (anti-)affinity on the VirtualMachine. An anti-affinity rule between HA "
            "peers that silently disappears is an outage waiting for a node failure.",
            {"rules": rules},
        )
    return None


@_rule
def secure_boot(f: VMFacts):
    if f.hardware.get("secure_boot"):
        return Finding(
            "vv.firmware.secure_boot",
            WARNING,
            "UEFI Secure Boot enabled",
            "VMs with Secure Boot enabled might fail to migrate automatically because "
            "Secure Boot prevents the VMs from booting on the destination provider (MTV-1548).",
            "Documented workaround: disable Secure Boot on the destination VM. If the "
            "guest also uses Measured Boot it cannot be migrated — rebuild it instead. "
            "A vTPM's contents are never transferred, so plan for BitLocker recovery keys.",
            {"firmware": f.hardware.get("firmware")},
        )
    return None


@_rule
def disk_serials(f: VMFacts):
    if f.hardware.get("enable_uuid") and any(
        "scsi" in (d.get("controller") or "").lower() for d in f.disks
    ):
        return Finding(
            "vmware.disk_serial.truncated",
            INFORMATION,
            "Disk serial numbers may be truncated",
            "This VM is configured with at least one SCSI disk and the disk.EnableUUID "
            "parameter is set to TRUE. Serial numbers will be truncated after migration.",
            "Only matters if something in the guest identifies disks by serial "
            "(multipath aliases, udev rules, some cluster software). Check those.",
        )
    return None


# forklift policies an RVTools export (as read today) cannot answer.
_NEVER_EVALUATED: tuple[Skipped, ...] = (
    Skipped("vmware.tpm.detected", "TPM detected", "RVTools sheets read today carry no vTPM flag"),
    Skipped(
        "vmware.passthrough_device.detected",
        "Passthrough device detected",
        "needs the vUSB/PCI device sheets, which are not read yet",
    ),
    Skipped("vmware.usb_controller.detected", "USB controller detected", "vUSB sheet not read yet"),
    Skipped("vmware.device.sriov.detected", "SR-IOV adapter detected", "not present in RVTools"),
    Skipped(
        "vmware.cpu_affinity.detected", "CPU affinity detected", "not present in RVTools vInfo"
    ),
    Skipped(
        "vmware.numa_affinity.detected", "NUMA node affinity detected", "not present in RVTools"
    ),
    Skipped(
        "vmware.drs.enabled", "VM running in a DRS-enabled cluster", "vCluster sheet not read yet"
    ),
    Skipped("vmware.dpm.enabled", "vSphere DPM detected", "vCluster sheet not read yet"),
    Skipped(
        "vv.fips.vsphere_version",
        "vSphere 6/7 VM to a FIPS cluster",
        "needs the vSphere version (vSource sheet) and a FIPS flag on the target cluster",
    ),
)

_SEVERITY_ORDER = {CRITICAL: 0, WARNING: 1, INFORMATION: 2}


def assess(facts: VMFacts) -> Assessment:
    if not facts.hardware:
        return Assessment(
            "unknown",
            [],
            [Skipped("*", "All rules", "this VM has no RVTools facts (it was added by hand)")],
        )
    findings: list[Finding] = []
    skipped: list[Skipped] = list(_NEVER_EVALUATED)
    for rule in _RULES:
        result = rule(facts)
        if isinstance(result, Finding):
            findings.append(result)
        elif isinstance(result, Skipped):
            skipped.append(result)
    findings.sort(key=lambda x: (_SEVERITY_ORDER[x.category], x.id))
    if any(x.category == CRITICAL for x in findings):
        status = "blocked"
    elif any(x.category == WARNING for x in findings):
        status = "warning"
    else:
        status = "ok"
    return Assessment(status, findings, skipped)


def blocks_migration_type(assessment: dict[str, Any] | None, migration_type: str) -> list[str]:
    """Finding ids that make this VM unfit for the given plan type."""
    out: list[str] = []
    for finding in (assessment or {}).get("findings") or []:
        if finding.get("category") == CRITICAL:
            out.append(finding["id"])
        elif migration_type == "warm" and finding.get("applies_to") == "warm":
            out.append(finding["id"])
    return out
