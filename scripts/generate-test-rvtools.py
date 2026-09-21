#!/usr/bin/env python3
"""Generate a realistic multi-sheet RVTools XLSX export for scale testing.

Emits the sheets and RVTools-4.x-style column headers the server-side
importer reads (vInfo / vDisk / vSnapshot / vNetwork / vCPU / vMemory,
plus header-only vDatastore / vHost), shaped like a federal hospital
estate:

  - several vCenters, two clusters each, four ESXi hosts per cluster
  - prod / staging / dev / dr mix, expressed the ways real sites express
    it (folder path, cluster name, custom attribute, name prefix)
  - prod, DMZ and management VLANs plus per-application VLANs
  - tiered datastores; db tiers on tier-1
  - HA families (``ehr-db-001`` … ``ehr-db-003``) so anti-affinity has
    something to spread
  - Windows + Linux guests with realistic OS strings, including ones MTV
    flags as unsupported
  - varied disk counts; a minority with snapshots, powered off, CBT off,
    RDM / independent / shared disks, vTPM-era EFI + secure boot, hot-add
  - the SAME VM name on two vCenters (names are unique per vCenter only)
  - a deliberate minority of malformed rows: missing name, over-long
    name, template, no datastore, no network, unicode name, in-file
    duplicate

Deterministic: identical arguments produce an identical workbook body
(``random.seed`` + crc32, never the salted built-in ``hash``).

Usage:
  scripts/generate-test-rvtools.py --vm-count 1000 --vcenter-count 3 \\
      --output tests/fixtures/rvtools-1000.xlsx

NOTE: the header names follow RVTools 4.x as documented publicly; they have
not been diffed against a specific customer export. The importer matches
headers through an alias table, so a version that renames a column needs
an alias, not a parser change.
"""

from __future__ import annotations

import argparse
import random
import sys
import zlib
from pathlib import Path

try:
    from openpyxl import Workbook
except ImportError:
    sys.stderr.write("openpyxl not installed. Install it with:\n  pip install openpyxl\n")
    raise SystemExit(2) from None


APPLICATIONS = [
    # (name, environment, tiers, network policy, weight)
    ("ehr", "prod", ["web", "app", "db"], "prod", 6),
    ("ehr", "staging", ["web", "app", "db"], "prod", 2),
    ("pacs", "prod", ["web", "app", "db"], "prod", 4),
    ("pacs", "staging", ["web", "app", "db"], "prod", 1),
    ("billing", "prod", ["web", "app", "db"], "prod", 3),
    ("sandbox", "dev", ["app"], "dmz", 4),
    ("patient-portal", "prod", ["web", "proxy"], "dmz", 2),
    ("identity", "prod", ["dc", "auth"], "mgmt", 2),
    ("reporting", "prod", ["app", "db"], "prod", 1),
    ("monitoring", "prod", ["app"], "mgmt", 2),
    ("backup", "dr", ["app"], "mgmt", 1),
]

OS_DISTRIBUTION = [
    ("Red Hat Enterprise Linux 9 (64-bit)", 0.34),
    ("Red Hat Enterprise Linux 8 (64-bit)", 0.18),
    ("Microsoft Windows Server 2022 (64-bit)", 0.18),
    ("Microsoft Windows Server 2019 (64-bit)", 0.10),
    ("Ubuntu Linux (64-bit)", 0.08),
    ("Microsoft Windows Server 2016 (64-bit)", 0.05),
    # Present in real estates; MTV's vm_os policy flags these.
    ("Microsoft Windows Server 2012 (64-bit)", 0.03),
    ("CentOS 7 (64-bit)", 0.03),
    ("Other 3.x or later Linux (64-bit)", 0.01),
]

VINFO_COLUMNS = [
    "VM", "Powerstate", "Template", "SRM Placeholder", "Config status", "DNS Name",
    "Connection state", "Guest state", "Consolidation Needed", "CPUs", "Memory", "NICs",
    "Disks", "Fixed Passthru HotPlug", "Latency Sensitivity", "EnableUUID", "CBT",
    "Primary IP Address", "Network #1", "Network #2", "Resource pool", "Folder", "vApp",
    "FT State", "Provisioned MiB", "In Use MiB", "Cluster rule name(s)", "EFI Secure boot",
    "Firmware", "HW version", "Path", "Annotation", "Environment", "Datacenter", "Cluster",
    "Host", "OS according to the configuration file", "OS according to the VMware Tools",
    "VM ID", "SMBIOS UUID", "VM UUID", "VI SDK Server",
]
VDISK_COLUMNS = [
    "VM", "Powerstate", "Template", "Disk", "Capacity MiB", "Raw", "Disk Mode",
    "Sharing mode", "Thin", "Controller", "Shared Bus", "Path", "Raw Comp. Mode",
    "VM ID", "VI SDK Server",
]
VSNAPSHOT_COLUMNS = [
    "VM", "Powerstate", "Name", "Description", "Date / time", "Size MiB (total)",
    "Quiesced", "VM ID", "VI SDK Server",
]
VNETWORK_COLUMNS = [
    "VM", "Powerstate", "Template", "NIC label", "Adapter", "Network", "Switch",
    "Connected", "Mac Address", "IPv4 Address", "VM ID", "VI SDK Server",
]
VCPU_COLUMNS = ["VM", "CPUs", "Hot Add", "Hot Remove", "VM ID", "VI SDK Server"]
VMEMORY_COLUMNS = ["VM", "Size MiB", "Hot Add", "VM ID", "VI SDK Server"]


def _crc(*parts: str) -> int:
    return zlib.crc32("|".join(parts).encode())


def _uuid(rng: random.Random) -> str:
    h = f"{rng.getrandbits(128):032x}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def _weighted(rng: random.Random, items, weight_index: int):
    return rng.choices(items, weights=[i[weight_index] for i in items], k=1)[0]


class Fleet:
    def __init__(self, vm_count: int, vcenter_count: int, seed: int, malformed: bool):
        self.rng = random.Random(seed)
        self.vm_count = vm_count
        self.malformed = malformed
        self.vcenters = [f"vc-site{i:02d}.hospital.example" for i in range(1, vcenter_count + 1)]
        self.vinfo: list[dict] = []
        self.vdisk: list[dict] = []
        self.vsnapshot: list[dict] = []
        self.vnetwork: list[dict] = []
        self.vcpu: list[dict] = []
        self.vmemory: list[dict] = []
        self.summary: dict[str, int] = {}
        self._family_seq: dict[tuple, int] = {}
        self._moref_seq = {vc: 1000 for vc in self.vcenters}

    def _bump(self, key: str) -> None:
        self.summary[key] = self.summary.get(key, 0) + 1

    def _placement(self, vc: str, environment: str) -> tuple[str, str, str]:
        site = vc.split(".")[0].removeprefix("vc-")
        # Non-prod shares a cluster whose NAME says so — tier 4 of env detection.
        cluster = f"{site}-prod-cl01" if environment in ("prod", "dr") else f"{site}-nonprod-cl02"
        host = f"esxi-{cluster}-{self.rng.randint(1, 4):02d}.hospital.example"
        return site, cluster, host

    def add_vm(self, idx: int) -> None:
        rng = self.rng
        app, environment, tiers, policy, _w = _weighted(rng, APPLICATIONS, 4)
        tier = rng.choice(tiers)
        vc = self.vcenters[idx % len(self.vcenters)]
        site, cluster, host = self._placement(vc, environment)

        fam = (vc, app, environment, tier)
        self._family_seq[fam] = self._family_seq.get(fam, 0) + 1
        env_tag = "" if environment == "prod" else f"{environment[:3]}-"
        name = f"{env_tag}{app}-{tier}-{self._family_seq[fam]:03d}"

        self._moref_seq[vc] += 1
        moref = f"vm-{self._moref_seq[vc]}"
        os_name = _weighted(rng, OS_DISTRIBUTION, 1)[0]
        is_windows = "Windows" in os_name
        powered_on = rng.random() > 0.06
        ds_prefix = site
        ds_tier = "tier1-ssd" if tier == "db" else rng.choice(["tier2-sas", "tier2-sas", "tier3-nl"])
        datastore = f"{ds_prefix}-{ds_tier}-{rng.randint(1, 3):02d}"
        primary_net = {"prod": "prod-vlan-100", "dmz": "dmz-vlan-200", "mgmt": "mgmt-vlan-50"}[policy]
        app_net = f"{app}-vlan-{110 + _crc(app) % 40}"
        networks = [primary_net] + ([app_net] if rng.random() < 0.6 else [])
        ip = f"10.{20 + self.vcenters.index(vc)}.{_crc(app, environment) % 250}.{5 + idx % 245}"

        # How this site expresses environment — exercise every detector tier.
        style = idx % 4
        folder = f"/{site}/{app}" if style == 1 else f"/{site}/{environment}/{app}"
        env_attr = environment if style == 2 else ""
        annotation = f"Owner: {app}-team@hospital.example | App: {app} | Tier: {tier}"

        efi = is_windows and rng.random() < 0.5
        disk_count = rng.choice([1, 1, 2, 2, 3, 4, 6])
        disks = []
        for d in range(disk_count):
            disks.append({
                "VM": name, "Powerstate": "poweredOn" if powered_on else "poweredOff",
                "Template": False, "Disk": f"Hard disk {d + 1}",
                "Capacity MiB": rng.choice([40960, 81920, 102400, 204800, 512000]),
                "Raw": False, "Disk Mode": "persistent", "Sharing mode": "sharingNone",
                "Thin": rng.random() < 0.7, "Controller": "SCSI controller 0",
                "Shared Bus": "noSharing",
                "Path": f"[{datastore}] {name}/{name}{'' if d == 0 else f'_{d}'}.vmdk",
                "Raw Comp. Mode": "", "VM ID": moref, "VI SDK Server": vc,
            })

        # Blockers / considerations MTV's validation policies key on.
        roll = rng.random()
        if roll < 0.02 and disk_count > 1:
            disks[-1].update({"Raw": True, "Raw Comp. Mode": "physicalMode",
                              "Disk Mode": "independent_persistent"})
            self._bump("rdm_disk")
        elif roll < 0.04 and disk_count > 1:
            disks[-1]["Disk Mode"] = "independent_persistent"
            self._bump("independent_disk")
        elif tier == "db" and disk_count > 1 and rng.random() < 0.08:
            disks[-1].update({"Sharing mode": "sharingMultiWriter", "Shared Bus": "physicalSharing",
                              "Controller": "SCSI controller 1"})
            self._bump("shared_disk")

        cbt = rng.random() < 0.8
        hot_add = rng.random() < 0.15
        has_snapshot = rng.random() < 0.08
        if not cbt:
            self._bump("cbt_off")
        if not powered_on:
            self._bump("powered_off")
        if has_snapshot:
            self._bump("with_snapshot")
            for s in range(rng.choice([1, 1, 2])):
                self.vsnapshot.append({
                    "VM": name, "Powerstate": "poweredOn" if powered_on else "poweredOff",
                    "Name": f"pre-patch-{s + 1}", "Description": "before monthly patching",
                    "Date / time": f"2026/0{rng.randint(1, 8)}/1{s} 02:00:00",
                    "Size MiB (total)": rng.randint(500, 40000), "Quiesced": False,
                    "VM ID": moref, "VI SDK Server": vc,
                })

        self.vinfo.append({
            "VM": name, "Powerstate": "poweredOn" if powered_on else "poweredOff",
            "Template": False, "SRM Placeholder": False, "Config status": "green",
            "DNS Name": f"{name}.hospital.example" if powered_on else "",
            "Connection state": "connected",
            "Guest state": "running" if powered_on else "notRunning",
            "Consolidation Needed": has_snapshot and rng.random() < 0.2,
            "CPUs": rng.choice([2, 4, 4, 8, 8, 16]),
            "Memory": rng.choice([4096, 8192, 16384, 32768, 65536]),
            "NICs": len(networks), "Disks": disk_count,
            "Fixed Passthru HotPlug": False, "Latency Sensitivity": "normal",
            "EnableUUID": is_windows, "CBT": cbt,
            "Primary IP Address": ip if powered_on else "",
            "Network #1": networks[0], "Network #2": networks[1] if len(networks) > 1 else "",
            "Resource pool": f"/{site}/Resources/{app}", "Folder": folder, "vApp": "",
            "FT State": "notConfigured",
            "Provisioned MiB": sum(d["Capacity MiB"] for d in disks),
            "In Use MiB": int(sum(d["Capacity MiB"] for d in disks) * rng.uniform(0.3, 0.9)),
            "Cluster rule name(s)": f"{app}-{tier}-separate" if tier == "db" else "",
            "EFI Secure boot": efi and rng.random() < 0.5,
            "Firmware": "efi" if efi else "bios",
            "HW version": rng.choice([13, 14, 15, 17, 19, 19, 21]),
            "Path": f"[{datastore}] {name}/{name}.vmx",
            "Annotation": annotation, "Environment": env_attr,
            "Datacenter": f"DC-{site.upper()}", "Cluster": cluster, "Host": host,
            "OS according to the configuration file": os_name,
            "OS according to the VMware Tools": os_name if powered_on else "",
            "VM ID": moref, "SMBIOS UUID": _uuid(rng), "VM UUID": _uuid(rng),
            "VI SDK Server": vc,
        })
        self.vdisk.extend(disks)
        for n, net in enumerate(networks):
            self.vnetwork.append({
                "VM": name, "Powerstate": "poweredOn" if powered_on else "poweredOff",
                "Template": False, "NIC label": f"Network adapter {n + 1}",
                "Adapter": "Vmxnet3" if rng.random() < 0.9 else "E1000e", "Network": net,
                "Switch": "dvs-core", "Connected": powered_on,
                "Mac Address": "00:50:56:%02x:%02x:%02x" % (rng.randrange(256), rng.randrange(256), rng.randrange(256)),
                "IPv4 Address": ip if (n == 0 and powered_on) else "",
                "VM ID": moref, "VI SDK Server": vc,
            })
        self.vcpu.append({"VM": name, "CPUs": self.vinfo[-1]["CPUs"], "Hot Add": hot_add,
                          "Hot Remove": False, "VM ID": moref, "VI SDK Server": vc})
        self.vmemory.append({"VM": name, "Size MiB": self.vinfo[-1]["Memory"], "Hot Add": hot_add,
                             "VM ID": moref, "VI SDK Server": vc})
        self._bump(f"env:{environment}")
        self._bump(f"vcenter:{vc}")

    def add_malformed(self) -> None:
        """Rows that must NOT abort the import. Counts in ``self.summary``
        under ``malformed:*`` are what the importer test asserts on."""
        vc = self.vcenters[0]
        base = dict(self.vinfo[0])

        def row(**over) -> dict:
            self._moref_seq[vc] += 1
            r = {**base, "VM ID": f"vm-{self._moref_seq[vc]}", "VI SDK Server": vc,
                 "VM UUID": _uuid(self.rng), "SMBIOS UUID": _uuid(self.rng)}
            r.update(over)
            return r

        # rejected
        self.vinfo.append(row(VM="", **{"DNS Name": ""}))
        self._bump("malformed:rejected")
        self.vinfo.append(row(VM="x" * 300))
        self._bump("malformed:rejected")
        self.vinfo.append(row(VM="rhel9-golden-template", Template=True))
        self._bump("malformed:rejected")
        dup = dict(self.vinfo[1])
        self.vinfo.append(dup)  # exact in-file duplicate (same MoRef)
        self._bump("malformed:rejected")
        # imported with a warning
        self.vinfo.append(row(VM="orphan-no-datastore-001", Path=""))
        self._bump("malformed:warning")
        self.vinfo.append(row(VM="isolated-no-network-001", **{"Network #1": "", "Network #2": ""}))
        self._bump("malformed:warning")
        # imported clean — valid, just unusual
        self.vinfo.append(row(VM="radiología-архив-画像-001"))
        self._bump("malformed:unicode_ok")
        if len(self.vcenters) > 1:
            # Same NAME as an existing vc1 VM, on vc2: legal, must import.
            other = self.vcenters[1]
            self._moref_seq[other] += 1
            self.vinfo.append({**base, "VM ID": f"vm-{self._moref_seq[other]}",
                               "VI SDK Server": other, "VM UUID": _uuid(self.rng)})
            self._bump("malformed:same_name_other_vcenter_ok")

    def build(self) -> None:
        extra = 3 if self.malformed else 0  # warning + unicode + cross-vc rows count as VMs
        if self.malformed and len(self.vcenters) > 1:
            extra += 1
        for idx in range(max(1, self.vm_count - extra)):
            self.add_vm(idx)
        if self.malformed:
            self.add_malformed()


def _write_sheet(wb: Workbook, title: str, columns: list[str], rows: list[dict], first: bool) -> None:
    ws = wb.active if first else wb.create_sheet(title)
    ws.title = title
    ws.append(columns)
    for r in rows:
        ws.append([r.get(c, "") for c in columns])


def write_xlsx(fleet: Fleet, output: Path) -> None:
    wb = Workbook()
    _write_sheet(wb, "vInfo", VINFO_COLUMNS, fleet.vinfo, first=True)
    _write_sheet(wb, "vCPU", VCPU_COLUMNS, fleet.vcpu, first=False)
    _write_sheet(wb, "vMemory", VMEMORY_COLUMNS, fleet.vmemory, first=False)
    _write_sheet(wb, "vDisk", VDISK_COLUMNS, fleet.vdisk, first=False)
    _write_sheet(wb, "vNetwork", VNETWORK_COLUMNS, fleet.vnetwork, first=False)
    _write_sheet(wb, "vSnapshot", VSNAPSHOT_COLUMNS, fleet.vsnapshot, first=False)
    _write_sheet(wb, "vDatastore", ["Name", "Type", "Capacity MiB", "Free MiB"], [], first=False)
    _write_sheet(wb, "vHost", ["Host", "CPU Model", "# Memory", "ESX Version"], [], first=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("--vm-count", type=int, default=1000,
                   help="VM rows that should IMPORT (rejected malformed rows are extra)")
    p.add_argument("--vcenter-count", type=int, default=3)
    p.add_argument("--output", type=Path, default=Path("/tmp/test-rvtools.xlsx"))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--no-malformed", action="store_true",
                   help="Skip the deliberate malformed-row block")
    args = p.parse_args(argv)

    fleet = Fleet(args.vm_count, args.vcenter_count, args.seed, not args.no_malformed)
    fleet.build()
    write_xlsx(fleet, args.output)

    print(f"==> Wrote {args.output} ({args.output.stat().st_size // 1024} KB), "
          f"{len(fleet.vinfo)} vInfo rows, {len(fleet.vdisk)} disks")
    for key in sorted(fleet.summary):
        print(f"    {key:42} {fleet.summary[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
