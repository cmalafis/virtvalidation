"""A DB-free snapshot of one target cluster's catalog.

The MTV emitter (:mod:`app.core.mtv`) and the plan pipeline are kept free
of database coupling, but deciding a NetworkMap destination needs more
than the mapping row: whether the target network is a *primary* UDN
(``type: pod``) or a secondary one (``multus``), and the StorageClass's
volume mode and copy-offload settings. Callers that hold a session load
a :class:`CatalogSnapshot` per cluster and hand it to the resolver.

A missing snapshot is legal — the resolver then behaves exactly as it
did before the catalog grew these attributes (every non-pod network is
``multus``, no volumeMode, no offload).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

_NETWORK_FIELDS = (
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
_STORAGE_FIELDS = (
    "access_mode",
    "volume_mode",
    "provisioner",
    "copy_offload_vendor",
    "copy_offload_secret",
)


@dataclass(frozen=True)
class CatalogSnapshot:
    ocp_target_id: int | None = None
    ocp_version: str | None = None
    mtv_version: str | None = None
    mtv_namespace: str | None = None
    # NNCP localnet names; None = never imported.
    localnet_bridge_mappings: list[str] | None = None
    networks: dict[str, dict[str, Any]] = field(default_factory=dict)
    storage_classes: dict[str, dict[str, Any]] = field(default_factory=dict)
    # namespace name → primary UDN network name (None when it has none)
    namespaces: dict[str, str | None] = field(default_factory=dict)

    def network(self, name: str | None) -> dict[str, Any] | None:
        return self.networks.get(name) if name else None

    def storage_class(self, name: str | None) -> dict[str, Any] | None:
        return self.storage_classes.get(name) if name else None


def _enum_value(v: Any) -> Any:
    return getattr(v, "value", v)


def load_catalog(db: Session, ocp_target_id: int | None) -> CatalogSnapshot | None:
    """Snapshot one cluster's catalog. None when the cluster doesn't exist."""
    from sqlalchemy import select

    from app.models.ocp_namespace import OCPTargetNamespace
    from app.models.target import OCPTarget
    from app.models.target_network import TargetNetwork
    from app.models.target_storage_class import TargetStorageClass

    if ocp_target_id is None:
        return None
    target = db.get(OCPTarget, ocp_target_id)
    if target is None:
        return None
    nets = db.scalars(select(TargetNetwork).where(TargetNetwork.ocp_target_id == ocp_target_id))
    scs = db.scalars(
        select(TargetStorageClass).where(TargetStorageClass.ocp_target_id == ocp_target_id)
    )
    nss = db.scalars(
        select(OCPTargetNamespace).where(OCPTargetNamespace.ocp_target_id == ocp_target_id)
    )
    return CatalogSnapshot(
        ocp_target_id=ocp_target_id,
        ocp_version=target.ocp_version,
        mtv_version=target.mtv_version,
        mtv_namespace=target.mtv_namespace,
        localnet_bridge_mappings=target.localnet_bridge_mappings,
        networks={n.name: {f: _enum_value(getattr(n, f)) for f in _NETWORK_FIELDS} for n in nets},
        storage_classes={
            s.name: {f: _enum_value(getattr(s, f)) for f in _STORAGE_FIELDS} for s in scs
        },
        namespaces={ns.name: ns.primary_udn_network for ns in nss},
    )


def load_catalogs_for(db: Session, mappings: list) -> dict[int, CatalogSnapshot]:
    """Snapshots for every cluster the mappings target, keyed by ocp_target_id."""
    out: dict[int, CatalogSnapshot] = {}
    for m in mappings or []:
        tid = getattr(m, "ocp_target_id", None)
        if tid is None or tid in out:
            continue
        snap = load_catalog(db, tid)
        if snap is not None:
            out[tid] = snap
    return out


def destination_kind(network: dict[str, Any] | None, row_type: str | None = None) -> str:
    """Classify a mapped target network for NetworkMap emission and the UI.

    Returns one of ``pod`` (cluster default network), ``primary_udn``
    (emitted as ``type: pod`` — MTV detects the UDN from the namespace),
    ``localnet`` / ``multus`` (both emitted as ``type: multus``).
    ``row_type`` is the mapping row's own ``target_network_type``, used
    when the catalog entry is gone or was never declared.
    """
    ntype = (network or {}).get("network_type") or row_type or "nad"
    if ntype == "pod":
        return "pod"
    role = (network or {}).get("role")
    if ntype in ("udn", "cudn") and role == "primary":
        return "primary_udn"
    if (network or {}).get("topology") == "localnet":
        return "localnet"
    return "multus"
