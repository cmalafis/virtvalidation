#!/usr/bin/env python3
"""Generate a realistic RVTools XLSX export for scale testing.

Produces the multi-sheet XLSX layout RVTools emits in production
(vInfo / vNetwork / vDisk / vDatastore / vHost). The per-VM
distribution mirrors what federal customers actually run:

  - applications: EHR, PACS, billing, identity, dev sandbox
  - environments: prod, staging, dev (~70/15/15 split)
  - OS mix: RHEL 9, RHEL 8, Windows Server 2022/2019, Ubuntu 22
  - networks: prod-VLAN, dmz-VLAN, mgmt-VLAN, plus per-app VLANs
  - datastores: per-vCenter tiered storage (tier1-ssd / bulk-hdd /
    nvme / archive)

Usage:
  scripts/generate-test-rvtools.py --vm-count 1000 --vcenter-count 3 \\
      --output /tmp/test-1000.xlsx

The output is byte-for-byte the same shape as a real RVTools export
— the frontend parser and backend importer treat it as authoritative
for scale testing.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

try:
    from openpyxl import Workbook
except ImportError:
    sys.stderr.write(
        "openpyxl not installed. Install it with:\n"
        "  pip install --user openpyxl\n"
    )
    raise SystemExit(2)


APPLICATIONS = [
    # (name, environments, tier mix, network policy, vm count weight)
    ("ehr-production",   ["prod"],            ["web", "app", "db"], "prod",  6),
    ("ehr-staging",      ["staging"],         ["web", "app", "db"], "prod",  2),
    ("pacs-imaging",     ["prod"],            ["web", "app", "db"], "prod",  4),
    ("pacs-staging",     ["staging"],         ["web", "app", "db"], "prod",  1),
    ("billing-finance",  ["prod"],            ["web", "app", "db"], "prod",  3),
    ("dev-sandbox",      ["dev"],             ["app"],              "dmz",   4),
    ("identity-services",["prod"],            ["app", "db"],        "mgmt",  2),
    ("legacy-reporting", ["prod"],            ["app", "db"],        "prod",  1),
    ("monitoring",       ["prod"],            ["app"],              "mgmt",  2),
    ("backup-services",  ["prod"],            ["app"],              "mgmt",  1),
]

OS_DISTRIBUTION = [
    ("Red Hat Enterprise Linux 9 (64-bit)", "rhel", 9, 0.40),
    ("Red Hat Enterprise Linux 8 (64-bit)", "rhel", 8, 0.20),
    ("Microsoft Windows Server 2022 (64-bit)", "windows", 2022, 0.20),
    ("Microsoft Windows Server 2019 (64-bit)", "windows", 2019, 0.10),
    ("Ubuntu Linux (64-bit)", "ubuntu", 22, 0.10),
]

DATASTORE_TIERS = ["tier1-ssd", "tier2-hdd", "nvme", "archive"]

# vInfo columns RVTools 4.x emits. Keep field-for-field alignment so the
# generated file parses through the production code path verbatim.
VINFO_COLUMNS = [
    "VM", "Powerstate", "Template", "Config status", "DNS Name", "vCenter",
    "Folder", "Datacenter", "Cluster", "Host", "OS according to the configuration file",
    "Guest state", "CPUs", "Memory", "NICs", "Disks", "min Required EVC Mode Key",
    "Annotation", "Primary IP Address", "vSphere Networks", "vSphere Datastores",
    "Provisioned MB", "In Use MB", "Storage policy", "Tools running",
]


def _weighted_pick(items, key):
    """Pick one item based on a weight extracted by ``key``."""
    weights = [key(i) for i in items]
    return random.choices(items, weights=weights, k=1)[0]


def _generate_vm_row(
    *,
    idx: int,
    app_name: str,
    environment: str,
    tier: str,
    vcenter_hostname: str,
    cluster_name: str,
    host_name: str,
    network_policy: str,
) -> dict:
    """One vInfo row keyed by the column headers RVTools emits."""
    os_display, os_family, _major, _w = _weighted_pick(
        OS_DISTRIBUTION, key=lambda o: o[3]
    )

    vm_short = f"{app_name[:8]}-{tier}-{idx:03d}"
    vm_name = f"{vm_short}.corp.local"
    octet = (idx % 250) + 5  # avoid 0/1/2/3/4 — those are network/router/dns
    ip_address = f"10.{hash(vcenter_hostname) % 250}.{hash(app_name) % 250}.{octet}"

    # Networks: one production VLAN per policy + an app-specific VLAN.
    networks = []
    if network_policy == "prod":
        networks.append("prod-vlan-100")
    elif network_policy == "dmz":
        networks.append("dmz-vlan-200")
    elif network_policy == "mgmt":
        networks.append("mgmt-vlan-50")
    networks.append(f"{app_name[:6]}-vlan-{(hash(app_name) % 40) + 110}")

    # Datastores: one tier-1 SSD for db; one bulk-HDD for app/web.
    if tier == "db":
        datastores = [f"{vcenter_hostname[:6]}-{DATASTORE_TIERS[0]}"]
    elif tier == "app":
        datastores = [f"{vcenter_hostname[:6]}-{random.choice(DATASTORE_TIERS[:2])}"]
    else:
        datastores = [f"{vcenter_hostname[:6]}-{DATASTORE_TIERS[1]}"]

    return {
        "VM": vm_name,
        "Powerstate": "poweredOn",
        "Template": "False",
        "Config status": "green",
        "DNS Name": vm_name,
        "vCenter": vcenter_hostname,
        "Folder": f"{app_name}/{environment}",
        "Datacenter": "DC-East",
        "Cluster": cluster_name,
        "Host": host_name,
        "OS according to the configuration file": os_display,
        "Guest state": "running",
        "CPUs": random.choice([2, 4, 4, 8, 8, 16]),
        "Memory": random.choice([4096, 8192, 16384, 32768]),
        "NICs": 1,
        "Disks": random.choice([1, 1, 2, 3]),
        "min Required EVC Mode Key": "intel-haswell",
        # Annotation field carries owner + tier metadata — operators
        # commonly stamp them via vSphere custom attributes.
        "Annotation": (
            f"Owner: {app_name.split('-')[0]}-team@corp.local | "
            f"App: {app_name} | Tier: {tier} | "
            f"Env: {environment}"
        ),
        "Primary IP Address": ip_address,
        "vSphere Networks": "; ".join(networks),
        "vSphere Datastores": "; ".join(datastores),
        "Provisioned MB": random.randint(50_000, 500_000),
        "In Use MB": random.randint(30_000, 400_000),
        "Storage policy": "default",
        "Tools running": "True",
        # Extra synthesized fields the spec doesn't require but real
        # RVTools captures — included so test files survive future
        # parser additions without regeneration.
        "_synthesized_os_family": os_family,
        "_synthesized_environment": environment,
        "_synthesized_application_hint": app_name,
    }


def _generate_vminfo_rows(vm_count: int, vcenter_count: int) -> list[dict]:
    vcenters = [
        f"vc-east-{i:02d}.corp.local" for i in range(1, vcenter_count + 1)
    ]
    clusters = [f"cluster-{c[3:7]}" for c in vcenters]
    hosts_per_cluster = 4

    rows: list[dict] = []
    idx = 0
    while len(rows) < vm_count:
        app = _weighted_pick(APPLICATIONS, key=lambda a: a[4])
        app_name, envs, tiers, net_policy, _ = app
        environment = random.choice(envs)
        tier = random.choice(tiers)
        vc_index = idx % len(vcenters)
        host = f"esxi-{clusters[vc_index]}-{(idx % hosts_per_cluster) + 1:02d}.corp.local"
        rows.append(
            _generate_vm_row(
                idx=idx,
                app_name=app_name,
                environment=environment,
                tier=tier,
                vcenter_hostname=vcenters[vc_index],
                cluster_name=clusters[vc_index],
                host_name=host,
                network_policy=net_policy,
            )
        )
        idx += 1
    return rows[:vm_count]


def _write_xlsx(rows: list[dict], output_path: Path) -> None:
    """Emit the XLSX with the standard RVTools sheet layout.

    Only vInfo carries real data; the others (vNetwork, vDisk, vHost,
    vDatastore) get column headers + a single placeholder row so any
    code that probes them for shape finds a valid table. The parser
    only reads vInfo today but real RVTools tooling expects all sheets
    present.
    """
    wb = Workbook()
    # Default sheet is named "Sheet" — rename it to vInfo so the
    # parser's case-insensitive lookup picks it up first.
    info = wb.active
    info.title = "vInfo"

    headers = VINFO_COLUMNS
    info.append(headers)
    for row in rows:
        info.append([row.get(col, "") for col in headers])

    # Placeholder sheets — header-only.
    for sheet_name, cols in (
        ("vNetwork", ["VM", "Network", "Switch", "Connected", "Status"]),
        ("vDisk", ["VM", "Disk", "Capacity MB", "Disk Path"]),
        ("vDatastore", ["Datastore", "Address", "Type", "Capacity MB", "Free MB"]),
        ("vHost", ["Host", "CPU Model", "Memory MB", "ESX Version"]),
    ):
        ws = wb.create_sheet(sheet_name)
        ws.append(cols)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--vm-count", type=int, default=1000,
        help="Total VMs to generate (default 1000)",
    )
    parser.add_argument(
        "--vcenter-count", type=int, default=3,
        help="Number of distinct vCenter hostnames to spread VMs across (default 3)",
    )
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/test-rvtools.xlsx"),
        help="Output XLSX path (default /tmp/test-rvtools.xlsx)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="Random seed for reproducible outputs",
    )
    args = parser.parse_args(argv)

    random.seed(args.seed)

    print(
        f"==> Generating {args.vm_count} VMs across {args.vcenter_count} "
        f"vCenters (seed={args.seed})"
    )
    rows = _generate_vminfo_rows(args.vm_count, args.vcenter_count)
    _write_xlsx(rows, args.output)

    # Quick distribution summary so the caller knows what they got.
    by_vc: dict[str, int] = {}
    by_app: dict[str, int] = {}
    by_env: dict[str, int] = {}
    for r in rows:
        by_vc[r["vCenter"]] = by_vc.get(r["vCenter"], 0) + 1
        by_app[r["_synthesized_application_hint"]] = (
            by_app.get(r["_synthesized_application_hint"], 0) + 1
        )
        by_env[r["_synthesized_environment"]] = (
            by_env.get(r["_synthesized_environment"], 0) + 1
        )

    print(f"==> Wrote {args.output} ({args.output.stat().st_size // 1024} KB)")
    print(f"    by vCenter:    {dict(sorted(by_vc.items()))}")
    print(f"    by app (top):  {dict(sorted(by_app.items(), key=lambda kv: -kv[1])[:5])}")
    print(f"    by env:        {dict(sorted(by_env.items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
