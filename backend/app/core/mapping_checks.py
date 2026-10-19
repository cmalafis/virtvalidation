"""Deterministic, version-aware checks on a resource mapping.

Stage 0 (:mod:`app.core.mapping_validation`) answers "is every source
network / datastore mapped?". These checks answer the next question —
"will what it is mapped to actually work on this cluster?" — using the
target catalog (:mod:`app.core.mapping_catalog`) and the OCP / MTV
support matrix (:mod:`app.core.platform_features`).

Pure Python, no I/O, no LLM. Each finding carries a ``severity``:

- ``blocking`` — the migration will fail or land the VM somewhere wrong
  (e.g. a static IP outside the primary UDN subnet with
  preserveStaticIPs on). The mapping preflight reports ``ok: false``.
- ``warning`` — works, but the operator should decide knowingly (Tech
  Preview features, VMs that won't live-migrate on RWO storage).
- ``info`` — context only.

Findings are reported per mapping (preflight) and per wave (the wave
card's network summary). Plan generation is never refused on them: the
operator owns the call, and Stage 0 keeps its existing contract.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from ipaddress import ip_address, ip_network
from typing import Any

from app.core.mapping_catalog import CatalogSnapshot, destination_kind
from app.core.platform_features import (
    COPY_OFFLOAD_DP_VENDORS,
    UDN_BATCH_WARN_THRESHOLD,
    effective_versions,
    feature_status,
)

SEVERITY_ORDER = {"blocking": 0, "warning": 1, "info": 2}


@dataclass
class MappingFinding:
    code: str
    severity: str  # blocking | warning | info
    message: str
    source: str | None = None  # source network / datastore
    target: str | None = None  # catalog network / StorageClass
    vm_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["vm_count"] = len(self.vm_names)
        d["vm_names"] = self.vm_names[:20]
        return d


@dataclass(frozen=True)
class VMView:
    """The few VM facts the checks read (decoupled from the ORM row)."""

    name: str
    vsphere_networks: tuple[str, ...] = ()
    vsphere_datastores: tuple[str, ...] = ()
    # (source network, ipv4) per NIC that reported an address
    nic_ips: tuple[tuple[str, str], ...] = ()
    target_namespace: str | None = None


def vm_view(vm: Any, namespace: str | None = None) -> VMView:
    """Build a :class:`VMView` from a VM ORM row (or anything shaped like one)."""
    nic_ips: list[tuple[str, str]] = []
    for nic in ((getattr(vm, "hardware_facts", None) or {}).get("nics")) or []:
        net = nic.get("network")
        for raw in str(nic.get("ipv4") or "").replace(";", ",").split(","):
            ip = raw.strip()
            if net and ip:
                nic_ips.append((net, ip))
    if not nic_ips and getattr(vm, "ip_address", None):
        nets = list(getattr(vm, "vsphere_networks", None) or [])
        if len(nets) == 1:
            nic_ips.append((nets[0], vm.ip_address))
    return VMView(
        name=getattr(vm, "name", "") or "",
        vsphere_networks=tuple(getattr(vm, "vsphere_networks", None) or ()),
        vsphere_datastores=tuple(getattr(vm, "vsphere_datastores", None) or ()),
        nic_ips=tuple(nic_ips),
        target_namespace=namespace,
    )


class _Collector:
    """Merges findings that share (code, source, target) and gathers VMs."""

    def __init__(self) -> None:
        self._by_key: dict[tuple, MappingFinding] = {}

    def add(
        self,
        code: str,
        severity: str,
        message: str,
        *,
        source: str | None = None,
        target: str | None = None,
        vm: str | None = None,
    ) -> None:
        key = (code, source, target)
        f = self._by_key.get(key)
        if f is None:
            f = MappingFinding(code, severity, message, source, target)
            self._by_key[key] = f
        if vm and vm not in f.vm_names:
            f.vm_names.append(vm)

    def result(self) -> list[MappingFinding]:
        return sorted(
            self._by_key.values(),
            key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.code, f.source or ""),
        )


def _in_any(ip: str, cidrs: Iterable[str]) -> bool | None:
    """True / False, or None when the address or every CIDR is unparseable."""
    try:
        addr = ip_address(ip)
    except ValueError:
        return None
    parsed = []
    for c in cidrs:
        try:
            parsed.append(ip_network(c, strict=False))
        except ValueError:
            continue
    if not parsed:
        return None
    return any(addr.version == n.version and addr in n for n in parsed)


def check_mapping(
    *,
    network_mappings: list[dict],
    storage_mappings: list[dict],
    catalog: CatalogSnapshot | None,
    vms: Iterable[VMView] = (),
    migration_type: str = "cold",
    preserve_static_ips: bool = True,
    datastore_types: dict[str, str] | None = None,
    provider_address: str | None = None,
) -> list[MappingFinding]:
    """Every finding for one mapping, optionally scoped to ``vms``.

    ``datastore_types`` maps a source datastore name to its vSphere type
    (``VMFS`` / ``NFS`` / ``vSAN`` / ``VVOL``) when known — copy offload
    does not work from NFS. ``provider_address`` is the vCenter's address;
    MTV fails the migration when it sits inside a primary UDN's subnet.
    """
    out = _Collector()
    cat = catalog or CatalogSnapshot()
    ocp, mtv = effective_versions(cat.ocp_version, cat.mtv_version)

    def status(key: str) -> str | None:
        return feature_status(key, ocp=ocp, mtv=mtv)

    vms = list(vms)

    net_rows = {r.get("source_network"): r for r in network_mappings or [] if isinstance(r, dict)}
    sc_rows = {r.get("source_datastore"): r for r in storage_mappings or [] if isinstance(r, dict)}

    if not cat.ocp_version:
        out.add(
            "target.version_unknown",
            "info",
            "The target cluster has no OpenShift version recorded, so feature support is "
            "judged against the newest release VirtValidate knows. Set it on the cluster.",
        )

    # --- Networks: per catalog entry the mapping uses --------------------
    used_networks: dict[str, list[str]] = {}
    for src, row in net_rows.items():
        if not src or row.get("ignored"):
            continue
        name = row.get("target_network_name")
        if name:
            used_networks.setdefault(name, []).append(src)

    for name, sources in used_networks.items():
        net = cat.network(name)
        if net is None:
            continue
        kind = destination_kind(net)
        src_label = ", ".join(sorted(sources))
        if kind == "primary_udn":
            if net.get("topology") not in (None, "layer2"):
                out.add(
                    "udn.primary_not_layer2",
                    "blocking",
                    f"Primary network {name!r} is {net.get('topology')}; VMs need a Layer2 "
                    "primary UDN.",
                    source=src_label,
                    target=name,
                )
            if net.get("ipam_mode") not in (None, "persistent"):
                out.add(
                    "udn.ipam_not_persistent",
                    "blocking",
                    f"Primary network {name!r} has ipam {net.get('ipam_mode')}; VMs need "
                    "ipam.lifecycle: Persistent to keep their IP across live migration.",
                    source=src_label,
                    target=name,
                )
            if net.get("binding") == "passt" and status("passt_binding") != "GA":
                out.add(
                    "udn.passt_tech_preview",
                    "warning",
                    f"{name!r} uses the passt binding, which is Tech Preview. MTV applies "
                    "l2bridge on migration; use l2bridge for production.",
                    target=name,
                )
            if net.get("transport") == "evpn":
                st = status("bgp_evpn_primary_cudn")
                if st is None:
                    out.add(
                        "udn.evpn_unavailable",
                        "blocking",
                        f"{name!r} uses EVPN transport, which needs OpenShift 4.22 or later.",
                        target=name,
                    )
                elif st != "GA":
                    out.add(
                        "udn.evpn_support_level",
                        "warning",
                        f"{name!r} uses BGP EVPN transport ({st}). Bare metal only; set at "
                        "CUDN creation. Confirm the support level for your release.",
                        target=name,
                    )
            subnets = net.get("subnets") or []
            if provider_address and subnets and _in_any(provider_address, subnets):
                out.add(
                    "udn.provider_ip_inside_subnet",
                    "blocking",
                    f"The vCenter address {provider_address} is inside {name!r} "
                    f"({', '.join(subnets)}). MTV fails migrations onto a UDN whose subnet "
                    "contains the source provider's IP; use a non-overlapping UDN subnet.",
                    source=src_label,
                    target=name,
                )
            st = status("mtv_udn_destination")
            if st and st != "GA":
                out.add(
                    "udn.mtv_destination_support",
                    "warning",
                    f"Migrating onto a primary UDN is {st} with this OpenShift / MTV release.",
                    target=name,
                )
        elif kind == "localnet":
            if net.get("ipam_mode") not in (None, "disabled", "none"):
                out.add(
                    "localnet.ipam_enabled",
                    "blocking",
                    f"Localnet network {name!r} has IPAM enabled; VMs on a localnet need "
                    "ipam.mode: Disabled.",
                    source=src_label,
                    target=name,
                )
            phys = net.get("physical_network_name")
            if not phys:
                out.add(
                    "localnet.no_physical_network",
                    "warning",
                    f"Localnet network {name!r} has no physicalNetworkName recorded, so it "
                    "can't be checked against the node bridge mappings.",
                    target=name,
                )
            elif (
                cat.localnet_bridge_mappings is not None
                and phys not in cat.localnet_bridge_mappings
            ):
                out.add(
                    "localnet.bridge_mapping_missing",
                    "blocking",
                    f"Localnet network {name!r} uses physicalNetworkName {phys!r}, but no "
                    "NodeNetworkConfigurationPolicy maps that localnet to a bridge. VMs "
                    "would have no layer-2 path off the node.",
                    source=src_label,
                    target=name,
                )

    # --- Per-VM network checks --------------------------------------------
    for vm in vms:
        for src in vm.vsphere_networks:
            row = net_rows.get(src)
            if not row or row.get("ignored") or not row.get("target_network_name"):
                continue
            net = cat.network(row.get("target_network_name"))
            if destination_kind(net, row.get("target_network_type")) != "primary_udn":
                continue
            name = row["target_network_name"]
            ns_udn = cat.namespaces.get(vm.target_namespace or "", None)
            if vm.target_namespace and vm.target_namespace in cat.namespaces and ns_udn != name:
                out.add(
                    "udn.namespace_not_primary",
                    "blocking",
                    f"{src!r} maps to primary network {name!r}, but namespace "
                    f"{vm.target_namespace!r} is not declared as served by it. A namespace "
                    "gets its primary UDN at creation (label "
                    "k8s.ovn.org/primary-user-defined-network); it cannot be added later.",
                    source=src,
                    target=name,
                    vm=vm.name,
                )
            subnets = (net or {}).get("subnets") or []
            if preserve_static_ips and subnets:
                for nic_net, ip in vm.nic_ips:
                    if nic_net != src:
                        continue
                    inside = _in_any(ip, subnets)
                    if inside is False:
                        out.add(
                            "udn.static_ip_outside_subnet",
                            "blocking",
                            f"VMs on {src!r} keep their static IP (preserveStaticIPs), but "
                            f"it is outside {name!r} ({', '.join(subnets)}). MTV fails these "
                            "VMs; widen the UDN subnet or re-IP them.",
                            source=src,
                            target=name,
                            vm=vm.name,
                        )

    # --- Storage ----------------------------------------------------------
    used_scs: dict[str, list[str]] = {}
    for src, row in sc_rows.items():
        name = row.get("target_storage_class")
        if src and name:
            used_scs.setdefault(name, []).append(src)

    for name, sources in used_scs.items():
        sc = cat.storage_class(name) or {}
        rows = [sc_rows[s] for s in sources]
        modes = {r.get("access_mode") or sc.get("access_mode") for r in rows}
        src_label = ", ".join(sorted(sources))
        if "ReadWriteOnce" in modes:
            out.add(
                "storage.rwo_no_live_migration",
                "warning",
                f"{name!r} is ReadWriteOnce. VMs on it cannot live-migrate between nodes "
                "(node drains and upgrades will shut them down). Use an RWX StorageClass "
                "for VMs that must stay up.",
                source=src_label,
                target=name,
            )
        vendor = sc.get("copy_offload_vendor")
        if vendor:
            if not sc.get("copy_offload_secret"):
                out.add(
                    "offload.no_secret",
                    "warning",
                    f"{name!r} declares copy offload ({vendor}) but no credentials Secret, so "
                    "the StorageMap is emitted without offload.",
                    target=name,
                )
            if vendor in COPY_OFFLOAD_DP_VENDORS:
                out.add(
                    "offload.vendor_developer_preview",
                    "warning",
                    f"Copy offload for {vendor} is Developer Preview — not supported for "
                    "production migrations.",
                    target=name,
                )
            key = "copy_offload_warm" if migration_type == "warm" else "copy_offload_cold"
            st = status(key)
            if st is None:
                out.add(
                    "offload.unavailable",
                    "blocking",
                    f"Copy offload needs MTV 2.10 or later; {name!r} declares {vendor}.",
                    target=name,
                )
            elif st != "GA":
                out.add(
                    "offload.support_level",
                    "warning",
                    f"Copy offload for {migration_type} migrations is {st} with this MTV "
                    "release (warm offload is GA from MTV 2.12).",
                    target=name,
                )
            for src in sources:
                if (datastore_types or {}).get(src, "").upper() == "NFS":
                    out.add(
                        "offload.nfs_datastore",
                        "warning",
                        f"Datastore {src!r} is NFS; copy offload does not work from NFS "
                        "datastores and MTV falls back to a VDDK copy.",
                        source=src,
                        target=name,
                    )

    # Per-VM guest caveats (long names vs live migration, RHEL static IPs)
    # live in app.core.assessment, so they show on the VM and in the wave's
    # considerations without being reported twice here.

    udn_count = sum(1 for n in cat.networks.values() if n.get("network_type") in ("udn", "cudn"))
    if udn_count > UDN_BATCH_WARN_THRESHOLD:
        out.add(
            "udn.many_networks",
            "info",
            f"The cluster declares {udn_count} UDN/CUDN networks. Creating more than "
            f"{UDN_BATCH_WARN_THRESHOLD} at once can drive nodes NotReady (MTV-5695); create "
            "them in batches ahead of the migration.",
        )

    return out.result()


def wave_mapping_view(
    vms: list[Any],
    resolver: Any,
    migration_type: str = "cold",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(summary, findings) for one wave, for the wave card.

    ``summary`` lists each distinct source network / datastore the wave's
    VMs use and where it lands; ``findings`` are :func:`check_mapping`
    results scoped to the rows and VMs of this wave. ``resolver`` is an
    :class:`app.core.mtv.MappingResolver` (or None, which yields an empty
    view — the wave has no mapping to describe).
    """
    if resolver is None:
        return {"networks": [], "storage": []}, []
    nets: dict[str, dict[str, Any]] = {}
    stores: dict[str, dict[str, Any]] = {}
    views: list[VMView] = []
    for vm in vms:
        payload = {
            "environment": getattr(vm, "environment", "") or "",
            "application_hint": getattr(vm, "application_hint", "") or "",
            "vcenter_folder": getattr(vm, "vsphere_folder", "") or "",
        }
        ns = getattr(vm, "target_namespace_override", None) or resolver.resolve_namespace(payload)
        views.append(vm_view(vm, ns))
        for src in getattr(vm, "vsphere_networks", None) or []:
            if not src:
                continue
            row = nets.get(src)
            if row is None:
                mapped = resolver.resolve_network(src)
                row = nets[src] = {
                    "source": src,
                    "kind": (mapped or {}).get("kind") or "unmapped",
                    "target": (mapped or {}).get("name") or None,
                    "vm_count": 0,
                }
            row["vm_count"] += 1
        for src in getattr(vm, "vsphere_datastores", None) or []:
            if not src:
                continue
            row = stores.get(src)
            if row is None:
                row = stores[src] = {
                    "source": src,
                    "storage_class": resolver.resolve_storage(src),
                    "access_mode": resolver.resolve_access_mode(src),
                    "volume_mode": resolver.resolve_volume_mode(src),
                    "offload": (resolver.resolve_offload(src) or {}).get("storageVendorProduct"),
                    "vm_count": 0,
                }
            row["vm_count"] += 1

    findings = check_mapping(
        network_mappings=[r for r in resolver.network_mappings if r.get("source_network") in nets],
        storage_mappings=[
            r for r in resolver.storage_mappings if r.get("source_datastore") in stores
        ],
        catalog=resolver.catalog,
        vms=views,
        migration_type=migration_type,
    )
    # The version note is a cluster-level fact; the mapping preflight shows it.
    findings = [f for f in findings if f.code != "target.version_unknown"]
    summary = {
        "networks": sorted(nets.values(), key=lambda r: r["source"]),
        "storage": sorted(stores.values(), key=lambda r: r["source"]),
    }
    return summary, [f.to_dict() for f in findings]
