"""What each OpenShift / MTV release supports, as data.

Single source of truth for version-dependent facts the mapping checks,
the recommender prompt, and the UI need: which OCP release pairs with
which MTV release, and the support level (GA / TP / DP) of the network
and storage features a migration plan can lean on. Pure data + lookups,
no I/O.

When a new OpenShift release lands, add its row to ``_OCP_TO_MTV`` and
extend the ``since`` / ``status_by_ocp`` entries in ``FEATURES`` — every
consumer picks it up. Every feature carries the URL it was verified
against; docs/MTV-GROUNDING.md §0 and §12 hold the long-form notes.

Retrieved 2026-10-18.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Vocabularies for the catalog attributes (validated by the Pydantic schemas,
# stored as plain strings — see app.models.target_network).
# ---------------------------------------------------------------------------
NETWORK_ROLES = ("primary", "secondary")
NETWORK_TOPOLOGIES = ("layer2", "layer3", "localnet", "bridge", "sriov", "other")
IPAM_MODES = ("persistent", "disabled", "none")
BINDINGS = ("l2bridge", "passt", "bridge", "sriov")
TRANSPORTS = ("geneve", "evpn")
VOLUME_MODES = ("Filesystem", "Block")

# forklift StorageMap offloadPlugin.vsphereXcopyConfig.storageVendorProduct
# (release-2.11 / release-2.12 Go types).
COPY_OFFLOAD_VENDORS = (
    "flashsystem",
    "vantara",
    "ontap",
    "primera3par",
    "pureFlashArray",
    "powerflex",
    "powermax",
    "powerstore",
    "infinibox",
)
# Vendors whose copy-offload path is Developer Preview (not supported).
COPY_OFFLOAD_DP_VENDORS = frozenset({"infinibox", "primera3par"})

# Known primary-UDN / CUDN gotchas with concrete thresholds.
UDN_BATCH_WARN_THRESHOLD = 72  # MTV-5695: >72 UDNs created at once → nodes NotReady
LIVE_MIGRATION_VM_NAME_MAX = 47  # CNV-61066: longer VM names fail live migration

_RN_421 = "https://docs.redhat.com/en/documentation/openshift_container_platform/4.21/html/virtualization/release-notes"
_RN_422 = "https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/virtualization/release-notes"
_NET_422 = "https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/virtualization/networking"
_EVPN_422 = "https://docs.redhat.com/en/documentation/openshift_container_platform/4.22/html/advanced_networking/bgp-evpn-for-user-defined-networks"
_MTV_211 = "https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.11/html-single/release_notes/index"
_MTV_212 = "https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.12/html/release_notes/ref_rn-2-12_release-notes"
_MTV_PLAN_211 = "https://docs.redhat.com/en/documentation/migration_toolkit_for_virtualization/2.11/pdf/planning_your_migration_to_red_hat_openshift_virtualization/Migration_Toolkit_for_Virtualization-2.11-Planning_your_migration_to_Red_Hat_OpenShift_Virtualization-en-US.pdf"

# Oldest → newest. MTV supports N-2 OCP releases; this is the release
# that *pairs* with each OCP minor (the default when an operator doesn't
# record the installed MTV version).
_OCP_TO_MTV: dict[tuple[int, int], tuple[int, int]] = {
    (4, 18): (2, 8),
    (4, 19): (2, 9),
    (4, 20): (2, 10),
    (4, 21): (2, 11),
    (4, 22): (2, 12),
}
SUPPORTED_OCP_VERSIONS = tuple(f"{a}.{b}" for a, b in _OCP_TO_MTV)
LATEST_OCP = max(_OCP_TO_MTV)


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    # (product, version) where it became available, product "ocp" | "mtv".
    since: tuple[str, tuple[int, int]]
    # Support level from ``since`` onward; ``status_by_version`` overrides
    # it for specific releases (e.g. TP in one release, GA in the next).
    status: str  # "GA" | "TP" | "DP"
    summary: str
    source: str
    status_by_version: dict[tuple[int, int], str] = field(default_factory=dict)


FEATURES: tuple[Feature, ...] = (
    Feature(
        key="primary_udn_layer2",
        title="Primary Layer2 UDN/CUDN for VMs",
        since=("ocp", (4, 18)),
        status="GA",
        summary="VMs on a primary Layer2 UDN keep a persistent IP across live "
        "migration (ipam.lifecycle: Persistent). MTV maps it as NetworkMap type pod "
        "and applies the l2bridge binding itself.",
        source=_NET_422,
    ),
    Feature(
        key="udn_import_preserve_ip_mac",
        title="Keep source IP + MAC when importing onto a primary L2 network",
        since=("ocp", (4, 21)),
        status="GA",
        summary="MTV keeps the source MAC and, with preserveStaticIPs, the static IP, "
        "when the VM lands on a primary Layer2 UDN (IP must sit inside the UDN subnet).",
        source=_RN_421,
    ),
    Feature(
        key="mtv_udn_destination",
        title="MTV migration onto preconfigured UDN endpoints",
        since=("ocp", (4, 20)),
        status="TP",
        summary="Migrating into a namespace whose primary network is a UDN. Tech "
        "Preview in the 4.20/4.21 feature tables; the 4.22 status is not published, "
        "so it is treated as TP until it is.",
        source="https://docs.redhat.com/documentation/openshift_container_platform/4.21/html-single/release_notes/index",
    ),
    Feature(
        key="localnet_cudn",
        title="Secondary localnet via ClusterUserDefinedNetwork",
        since=("ocp", (4, 18)),
        status="GA",
        summary="topology: Localnet, role: Secondary; physicalNetworkName must match an "
        "NNCP ovn.bridge-mappings localnet; ipam.mode: Disabled for VMs. "
        "Console create/manage added in 4.21. Localnet NADs remain supported.",
        source=_RN_421,
    ),
    Feature(
        key="passt_binding",
        title="passt network binding",
        since=("ocp", (4, 18)),
        status="TP",
        summary="Alternative to l2bridge on a primary UDN. Tech Preview — use l2bridge "
        "for production.",
        source=_NET_422,
    ),
    Feature(
        key="bgp_evpn_primary_cudn",
        title="BGP EVPN transport for primary CUDN",
        since=("ocp", (4, 22)),
        status="TP",
        summary="spec.network.transport: EVPN stretches a VMware L2 segment into the "
        "cluster. Bare metal only, IPv4 VTEPs, set at CUDN creation (immutable). "
        "The 4.22 notes give no support level, so it is treated as TP until confirmed.",
        source=_EVPN_422,
    ),
    Feature(
        key="ipv6_single_stack_vms",
        title="IPv6 single-stack VMs on secondary networks",
        since=("ocp", (4, 22)),
        status="GA",
        summary="Localnet, Linux bridge and SR-IOV secondary networks.",
        source=_RN_422,
    ),
    Feature(
        key="live_nad_swap",
        title="Change a VM's secondary network without downtime",
        since=("ocp", (4, 22)),
        status="GA",
        summary="Editing the VM's multus networkName live-migrates it onto the new NAD, "
        "so a post-cutover mapping fix no longer needs a restart.",
        source=_RN_422,
    ),
    Feature(
        key="storage_live_migration",
        title="Native storage live migration",
        since=("ocp", (4, 21)),
        status="GA",
        summary="Move VM disks to another StorageClass while running; replaces MTC "
        "(end of life). 4.22 adds optional source-PVC cleanup.",
        source=_RN_421,
    ),
    Feature(
        key="copy_offload_cold",
        title="Storage copy offload (vSphere XCOPY), cold migration",
        since=("mtv", (2, 10)),
        status="TP",
        status_by_version={(2, 11): "GA", (2, 12): "GA"},
        summary="StorageMap offloadPlugin.vsphereXcopyConfig: the array copies the disk "
        "instead of VDDK. Not on NFS datastores.",
        source=_MTV_211,
    ),
    Feature(
        key="copy_offload_warm",
        title="Storage copy offload (vSphere XCOPY), warm migration",
        since=("mtv", (2, 10)),
        status="DP",
        status_by_version={(2, 11): "TP", (2, 12): "GA"},
        summary="Warm migrations with copy offload.",
        source=_MTV_212,
    ),
    Feature(
        key="multi_nic_same_network",
        title="Several NICs on one source network → different targets",
        since=("mtv", (2, 12)),
        status="GA",
        summary="Repeated NetworkMap entries for one source network (backported to 2.11.7).",
        source=_MTV_212,
    ),
    Feature(
        key="cross_cluster_live_migration",
        title="OpenShift-to-OpenShift live migration (Plan type: live)",
        since=("ocp", (4, 21)),
        status="TP",
        summary="GA on the OpenShift Virtualization side from 4.21.1, but MTV has not "
        "relabelled it since the 2.10 Tech Preview, so it is treated as TP.",
        source=_MTV_PLAN_211,
    ),
)
_FEATURES_BY_KEY = {f.key: f for f in FEATURES}


def parse_version(raw: str | None) -> tuple[int, int] | None:
    """``"4.21.30"`` / ``"v4.22"`` / ``"4.21"`` → ``(4, 21)``. ``None`` if unparseable."""
    if not raw:
        return None
    m = re.search(r"(\d+)\.(\d+)", str(raw))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def mtv_for(ocp_version: str | None) -> tuple[int, int] | None:
    """The MTV release paired with an OCP minor (clamped to the known range)."""
    v = parse_version(ocp_version)
    if v is None:
        return None
    if v in _OCP_TO_MTV:
        return _OCP_TO_MTV[v]
    if v > LATEST_OCP:
        return _OCP_TO_MTV[LATEST_OCP]
    return None


def effective_versions(
    ocp_version: str | None, mtv_version: str | None = None
) -> tuple[tuple[int, int] | None, tuple[int, int] | None]:
    """(ocp, mtv) for a target. MTV falls back to the pairing for the OCP version."""
    ocp = parse_version(ocp_version)
    mtv = parse_version(mtv_version) or mtv_for(ocp_version)
    return ocp, mtv


def feature_status(
    key: str, *, ocp: tuple[int, int] | None, mtv: tuple[int, int] | None
) -> str | None:
    """Support level of ``key`` on this target: "GA" / "TP" / "DP", or None
    if the release predates it. Unknown versions assume the newest known
    release — operators who never filled in a version get today's answer,
    and the UI flags the missing version separately."""
    f = _FEATURES_BY_KEY[key]
    product, since = f.since
    version = ocp if product == "ocp" else mtv
    if version is None:
        version = LATEST_OCP if product == "ocp" else _OCP_TO_MTV[LATEST_OCP]
    if version < since:
        return None
    return f.status_by_version.get(version, f.status)


def features_for(ocp_version: str | None, mtv_version: str | None = None) -> list[dict]:
    """Every feature with its status on this target, for the API / UI."""
    ocp, mtv = effective_versions(ocp_version, mtv_version)
    out = []
    for f in FEATURES:
        out.append(
            {
                "key": f.key,
                "title": f.title,
                "status": feature_status(f.key, ocp=ocp, mtv=mtv) or "unavailable",
                "summary": f.summary,
                "source": f.source,
            }
        )
    return out


def prompt_context(ocp_version: str | None, mtv_version: str | None = None) -> list[str]:
    """Short "what this cluster supports" lines for an LLM prompt (≤6 lines)."""
    ocp, mtv = effective_versions(ocp_version, mtv_version)
    label = f"OpenShift {ocp[0]}.{ocp[1]}" if ocp else "OpenShift (version not recorded)"
    lines = [f"Target: {label}" + (f", MTV {mtv[0]}.{mtv[1]}" if mtv else "")]
    for key in (
        "primary_udn_layer2",
        "udn_import_preserve_ip_mac",
        "localnet_cudn",
        "passt_binding",
        "bgp_evpn_primary_cudn",
    ):
        st = feature_status(key, ocp=ocp, mtv=mtv)
        if st:
            lines.append(f"- {_FEATURES_BY_KEY[key].title}: {st}")
    return lines[:6]
