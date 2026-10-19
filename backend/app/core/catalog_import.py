"""Build a target-cluster catalog from pasted ``oc get … -o yaml`` output.

VirtValidate never connects to a cluster (air-gapped by design), so the
operator declares what the cluster offers. Typing a dozen NADs, UDNs and
StorageClasses by hand is where mappings go wrong; this module parses the
operator's own export instead::

    oc get network-attachment-definitions,userdefinednetworks -A -o yaml
    oc get clusteruserdefinednetworks,storageclasses,storageprofiles,\\
        nodenetworkconfigurationpolicies -o yaml
    oc get namespaces -l k8s.ovn.org/primary-user-defined-network -o yaml

Pure Python, deterministic, no I/O. :func:`parse_cluster_yaml` returns
the catalog entries it derived; :func:`plan_import` diffs them against
the current catalog so the UI can preview creates / updates before the
API applies them.

Derivation rules (docs/MTV-GROUNDING.md §12):

- **NetworkAttachmentDefinition** — ``spec.config`` CNI JSON. ``bridge``
  / ``cnv-bridge`` → topology ``bridge`` (+ ``vlan``);
  ``ovn-k8s-cni-overlay`` → its ``topology`` (localnet / layer2 / layer3)
  with ``vlanID`` / ``physicalNetworkName`` / ``subnets``; ``sriov`` →
  ``sriov``. NADs owned by a UDN/CUDN are skipped — the UDN entry
  describes them.
- **UserDefinedNetwork** / **ClusterUserDefinedNetwork** — role, topology,
  subnets, ``ipam.lifecycle: Persistent`` → ``persistent``,
  ``ipam.mode: Disabled`` → ``disabled``, ``transport: EVPN`` → ``evpn``.
  A primary UDN marks its namespace; a primary CUDN marks the namespaces
  its selector matches among pasted Namespace objects, or those listed in
  its ``NetworkCreated`` status condition.
- **StorageClass** — provisioner, default-class annotations; volume and
  access mode from the matching CDI **StorageProfile** when pasted
  (``status.claimPropertySets[0]``), else a provisioner heuristic.
- **NodeNetworkConfigurationPolicy** — ``ovn.bridge-mappings[].localnet``
  names, which every localnet ``physicalNetworkName`` must match.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

MAX_IMPORT_BYTES = 5 * 1024 * 1024

_PRIMARY_UDN_LABEL = "k8s.ovn.org/primary-user-defined-network"
_DEFAULT_SC_ANNOTATIONS = (
    "storageclass.kubernetes.io/is-default-class",
    "storageclass.kubevirt.io/is-default-virt-class",
)
_OWNED_BY_UDN = {"UserDefinedNetwork", "ClusterUserDefinedNetwork"}
_ACCESS_MODES = ("ReadWriteOnce", "ReadWriteMany", "ReadOnlyMany")

# (provisioner substring, volume_mode, access_mode) — used only when no
# StorageProfile was pasted. Conservative: unknown provisioners get
# neither, and the StorageProfile decides at migration time.
_PROVISIONER_DEFAULTS = (
    ("rbd.csi.ceph.com", "Block", "ReadWriteMany"),
    ("cephfs.csi.ceph.com", "Filesystem", "ReadWriteMany"),
    ("nfs.csi.k8s.io", "Filesystem", "ReadWriteMany"),
    ("ebs.csi.aws.com", "Block", "ReadWriteOnce"),
    ("pd.csi.storage.gke.io", "Block", "ReadWriteOnce"),
    ("disk.csi.azure.com", "Block", "ReadWriteOnce"),
)


class CatalogImportError(ValueError):
    """The pasted text could not be parsed as Kubernetes YAML/JSON."""


@dataclass
class ImportedNetwork:
    name: str
    network_type: str  # nad | udn | cudn
    namespace: str | None = None
    role: str | None = None
    topology: str | None = None
    vlan_id: int | None = None
    physical_network_name: str | None = None
    mtu: int | None = None
    subnets: list[str] | None = None
    ipam_mode: str | None = None
    binding: str | None = None
    transport: str | None = None


@dataclass
class ImportedStorageClass:
    name: str
    provisioner: str | None = None
    access_mode: str = "ReadWriteOnce"
    volume_mode: str | None = None
    is_default: bool = False


@dataclass
class ParsedCluster:
    networks: list[ImportedNetwork] = field(default_factory=list)
    storage_classes: list[ImportedStorageClass] = field(default_factory=list)
    # namespace → primary UDN/CUDN network name
    primary_udn_namespaces: dict[str, str] = field(default_factory=dict)
    localnet_bridge_mappings: list[str] | None = None
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _documents(text: str) -> list[dict]:
    if len(text.encode("utf-8", "ignore")) > MAX_IMPORT_BYTES:
        raise CatalogImportError(
            f"Pasted text is larger than {MAX_IMPORT_BYTES // 1024 // 1024} MB"
        )
    try:
        raw = list(yaml.safe_load_all(text))
    except yaml.YAMLError as e:
        raise CatalogImportError(f"Not valid YAML/JSON: {str(e)[:300]}") from e
    out: list[dict] = []
    for doc in raw:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind") == "List" or (isinstance(doc.get("items"), list) and "kind" not in doc):
            out.extend(i for i in doc.get("items") or [] if isinstance(i, dict))
        elif str(doc.get("kind") or "").endswith("List") and isinstance(doc.get("items"), list):
            out.extend(i for i in doc["items"] if isinstance(i, dict))
        else:
            out.append(doc)
    return out


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _subnets(v: Any) -> list[str] | None:
    """UDN subnets are a list (Layer2: CIDRs; Layer3: {cidr, hostSubnet});
    NAD config carries a comma-separated string."""
    if not v:
        return None
    items = v.split(",") if isinstance(v, str) else v if isinstance(v, list) else [v]
    out = []
    for item in items:
        cidr = item.get("cidr") if isinstance(item, dict) else item
        if cidr and str(cidr).strip():
            out.append(str(cidr).strip())
    return out or None


def _ipam(ipam: Any) -> str | None:
    if not isinstance(ipam, dict):
        return None
    if str(ipam.get("mode") or "").lower() == "disabled":
        return "disabled"
    if str(ipam.get("lifecycle") or "").lower() == "persistent":
        return "persistent"
    return None


def _owned_by_udn(meta: dict) -> bool:
    return any(
        (ref or {}).get("kind") in _OWNED_BY_UDN for ref in meta.get("ownerReferences") or []
    )


def _parse_nad(doc: dict) -> ImportedNetwork | str:
    meta = doc.get("metadata") or {}
    name = meta.get("name")
    if not name:
        return "NetworkAttachmentDefinition without a name skipped"
    raw = (doc.get("spec") or {}).get("config") or "{}"
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return f"NAD {name!r}: spec.config is not JSON — skipped"
    # A conflist nests the interesting plugin under "plugins".
    plugin = cfg
    if isinstance(cfg.get("plugins"), list) and cfg["plugins"]:
        plugin = cfg["plugins"][0] or {}
    cni = str(plugin.get("type") or "")
    net = ImportedNetwork(
        name=name, network_type="nad", namespace=meta.get("namespace"), role="secondary"
    )
    net.mtu = _int(plugin.get("mtu"))
    if cni in ("bridge", "cnv-bridge"):
        net.topology = "bridge"
        net.binding = "bridge"
        net.vlan_id = _int(plugin.get("vlan"))
    elif cni == "ovn-k8s-cni-overlay":
        topo = str(plugin.get("topology") or "").lower()
        net.topology = topo if topo in ("localnet", "layer2", "layer3") else "other"
        net.vlan_id = _int(plugin.get("vlanID"))
        net.physical_network_name = plugin.get("physicalNetworkName") or (
            # Before physicalNetworkName existed, the localnet name was the
            # NAD config name.
            plugin.get("name") if net.topology == "localnet" else None
        )
        net.subnets = _subnets(plugin.get("subnets"))
        net.ipam_mode = "none" if net.topology == "localnet" and not net.subnets else None
        if str(plugin.get("role") or "").lower() == "primary":
            net.role = "primary"
    elif cni == "sriov":
        net.topology = "sriov"
        net.binding = "sriov"
        net.vlan_id = _int(plugin.get("vlan"))
    else:
        net.topology = "other"
    return net


def _parse_udn(doc: dict, cluster_scoped: bool) -> ImportedNetwork | str:
    meta = doc.get("metadata") or {}
    name = meta.get("name")
    if not name:
        return "UDN without a name skipped"
    spec = doc.get("spec") or {}
    netspec = (spec.get("network") or {}) if cluster_scoped else spec
    topo = str(netspec.get("topology") or "").lower()
    body = netspec.get(topo) or netspec.get(topo.capitalize()) or {}
    if not isinstance(body, dict):
        body = {}
    net = ImportedNetwork(
        name=name,
        network_type="cudn" if cluster_scoped else "udn",
        namespace=None if cluster_scoped else meta.get("namespace"),
        role=str(body.get("role") or "").lower() or None,
        topology=topo if topo in ("layer2", "layer3", "localnet") else "other",
        subnets=_subnets(body.get("subnets")),
        ipam_mode=_ipam(body.get("ipam")),
        mtu=_int(body.get("mtu")),
    )
    if topo == "localnet":
        net.physical_network_name = body.get("physicalNetworkName")
        vlan = body.get("vlan") or {}
        if isinstance(vlan, dict):
            net.vlan_id = _int((vlan.get("access") or {}).get("id"))
    if str(netspec.get("transport") or spec.get("transport") or "").lower() == "evpn":
        net.transport = "evpn"
    if net.role == "primary":
        net.binding = "l2bridge"
    return net


_NS_LIST_RE = re.compile(r"\[([^\]]*)\]")


def _cudn_status_namespaces(doc: dict) -> list[str]:
    for cond in (doc.get("status") or {}).get("conditions") or []:
        msg = str((cond or {}).get("message") or "")
        m = _NS_LIST_RE.search(msg)
        if m and "namespace" in msg.lower():
            return [n.strip().strip("\"'") for n in re.split(r"[,\s]+", m.group(1)) if n.strip()]
    return []


def _selector_matches(selector: dict, labels: dict) -> bool:
    if not isinstance(selector, dict) or not selector:
        return False
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for expr in selector.get("matchExpressions") or []:
        key, op, values = expr.get("key"), expr.get("operator"), expr.get("values") or []
        if op == "In" and labels.get(key) not in values:
            return False
        if op == "NotIn" and labels.get(key) in values:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _storage_profile_defaults(doc: dict) -> tuple[str | None, str | None]:
    sets = (
        (doc.get("status") or {}).get("claimPropertySets")
        or (doc.get("spec") or {}).get("claimPropertySets")
        or []
    )
    for s in sets:
        modes = [m for m in (s or {}).get("accessModes") or [] if m in _ACCESS_MODES]
        vm = (s or {}).get("volumeMode")
        if modes or vm:
            access = "ReadWriteMany" if "ReadWriteMany" in modes else (modes[0] if modes else None)
            return access, vm if vm in ("Filesystem", "Block") else None
    return None, None


def parse_cluster_yaml(text: str) -> ParsedCluster:
    """Everything the pasted export says about networks, storage and
    namespaces. Unknown kinds are counted and ignored."""
    docs = _documents(text)
    result = ParsedCluster()
    counts: dict[str, int] = {}
    namespaces: dict[str, dict] = {}
    profiles: dict[str, tuple[str | None, str | None]] = {}
    cudn_docs: list[tuple[ImportedNetwork, dict]] = []
    bridge_mappings: set[str] | None = None
    seen_names: dict[str, ImportedNetwork] = {}

    def add_network(net: ImportedNetwork) -> None:
        prior = seen_names.get(net.name)
        if prior is not None:
            result.warnings.append(
                f"Network name {net.name!r} appears more than once "
                f"({prior.network_type} in {prior.namespace or 'cluster scope'}, "
                f"{net.network_type} in {net.namespace or 'cluster scope'}); kept the first. "
                "Catalog names are unique per cluster."
            )
            return
        seen_names[net.name] = net
        result.networks.append(net)

    for doc in docs:
        kind = str(doc.get("kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
        meta = doc.get("metadata") or {}
        if kind == "Namespace":
            namespaces[meta.get("name") or ""] = meta.get("labels") or {}
        elif kind == "StorageProfile":
            profiles[meta.get("name") or ""] = _storage_profile_defaults(doc)
        elif kind == "NodeNetworkConfigurationPolicy":
            ovn = ((doc.get("spec") or {}).get("desiredState") or {}).get("ovn") or {}
            names = {
                m.get("localnet")
                for m in ovn.get("bridge-mappings") or []
                if isinstance(m, dict) and m.get("localnet") and m.get("state") != "absent"
            }
            bridge_mappings = (bridge_mappings or set()) | names
    # Second pass: networks + storage need the profiles / namespaces above.
    for doc in docs:
        kind = str(doc.get("kind") or "")
        meta = doc.get("metadata") or {}
        if kind == "NetworkAttachmentDefinition":
            if _owned_by_udn(meta):
                continue
            parsed = _parse_nad(doc)
        elif kind in ("UserDefinedNetwork", "ClusterUserDefinedNetwork"):
            parsed = _parse_udn(doc, cluster_scoped=kind == "ClusterUserDefinedNetwork")
            if isinstance(parsed, ImportedNetwork) and kind == "ClusterUserDefinedNetwork":
                cudn_docs.append((parsed, doc))
        elif kind == "StorageClass":
            name = meta.get("name")
            if not name:
                continue
            prov = doc.get("provisioner")
            access, volume = profiles.get(name, (None, None))
            if access is None and volume is None:
                for needle, v, a in _PROVISIONER_DEFAULTS:
                    if prov and needle in prov:
                        volume, access = v, a
                        break
            ann = meta.get("annotations") or {}
            result.storage_classes.append(
                ImportedStorageClass(
                    name=name,
                    provisioner=prov,
                    access_mode=access or "ReadWriteOnce",
                    volume_mode=volume,
                    is_default=any(
                        str(ann.get(a)).lower() == "true" for a in _DEFAULT_SC_ANNOTATIONS
                    ),
                )
            )
            continue
        else:
            continue
        if isinstance(parsed, str):
            result.warnings.append(parsed)
        else:
            add_network(parsed)
            if parsed.role == "primary" and parsed.network_type == "udn" and parsed.namespace:
                result.primary_udn_namespaces[parsed.namespace] = parsed.name

    for net, doc in cudn_docs:
        if net.role != "primary":
            continue
        selector = (doc.get("spec") or {}).get("namespaceSelector") or {}
        matched = [
            ns for ns, labels in namespaces.items() if ns and _selector_matches(selector, labels)
        ]
        for ns in matched or _cudn_status_namespaces(doc):
            result.primary_udn_namespaces.setdefault(ns, net.name)

    # A pasted Namespace with the primary-UDN label but no network found.
    for ns, labels in namespaces.items():
        if ns and _PRIMARY_UDN_LABEL in labels and ns not in result.primary_udn_namespaces:
            result.warnings.append(
                f"Namespace {ns!r} carries {_PRIMARY_UDN_LABEL} but no primary UDN/CUDN for it "
                "was in the paste."
            )

    if bridge_mappings is not None:
        result.localnet_bridge_mappings = sorted(bridge_mappings)
    for net in result.networks:
        if (
            net.topology == "localnet"
            and net.physical_network_name
            and bridge_mappings is not None
            and net.physical_network_name not in bridge_mappings
        ):
            result.warnings.append(
                f"Localnet {net.name!r} uses physicalNetworkName {net.physical_network_name!r}, "
                "which no pasted NodeNetworkConfigurationPolicy maps to a bridge."
            )
    result.counts = {k: v for k, v in counts.items() if k}
    return result


# ---------------------------------------------------------------------------
# Diff against the current catalog
# ---------------------------------------------------------------------------
_NET_DIFF_FIELDS = (
    "network_type",
    "namespace",
    "role",
    "topology",
    "vlan_id",
    "physical_network_name",
    "mtu",
    "subnets",
    "ipam_mode",
    "binding",
    "transport",
)
_SC_DIFF_FIELDS = ("provisioner", "access_mode", "volume_mode")


def _diff(new: dict, current: dict | None, fields: tuple[str, ...]) -> tuple[str, list[str]]:
    if current is None:
        return "create", list(fields)
    changed = [f for f in fields if new.get(f) is not None and new.get(f) != current.get(f)]
    return ("update" if changed else "unchanged"), changed


def plan_import(
    parsed: ParsedCluster,
    *,
    current_networks: dict[str, dict],
    current_storage: dict[str, dict],
    current_namespaces: dict[str, str | None],
) -> dict[str, Any]:
    """Preview: every derived entry with ``action`` create / update /
    unchanged and the fields that would change. Imported values never
    clear a field the operator set — ``None`` means "not in the paste"."""
    networks = []
    for net in parsed.networks:
        row = asdict(net)
        row["action"], row["changed"] = _diff(row, current_networks.get(net.name), _NET_DIFF_FIELDS)
        networks.append(row)
    storage = []
    for sc in parsed.storage_classes:
        row = asdict(sc)
        row["action"], row["changed"] = _diff(row, current_storage.get(sc.name), _SC_DIFF_FIELDS)
        if sc.is_default and not (current_storage.get(sc.name) or {}).get("is_default"):
            row["changed"].append("is_default")
            if row["action"] == "unchanged":
                row["action"] = "update"
        storage.append(row)
    namespaces = []
    for ns, udn in sorted(parsed.primary_udn_namespaces.items()):
        if ns not in current_namespaces:
            action = "create"
        elif current_namespaces[ns] != udn:
            action = "update"
        else:
            action = "unchanged"
        namespaces.append({"name": ns, "primary_udn_network": udn, "action": action})
    return {
        "networks": networks,
        "storage_classes": storage,
        "namespaces": namespaces,
        "localnet_bridge_mappings": parsed.localnet_bridge_mappings,
        "warnings": list(parsed.warnings),
        "counts": dict(parsed.counts),
    }
