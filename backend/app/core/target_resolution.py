"""Per-VM target resolution.

Single source of truth for "for VM X, what cluster does it land on,
what namespace, which target network does each source network become,
which target StorageClass does each source datastore become?"

Resolution is **hybrid**:

  * Per-VM overrides are persisted on the VM row
    (``target_cluster_id_override`` and ``target_namespace_override``).
    When set, they win unconditionally.
  * Everything else is inferred at read time from the matching
    ``ResourceMapping`` for the VM's ``(source_vcenter_id,
    target_cluster_id)`` pair. Mappings are unique per pair (see
    ``uq_mapping_per_pair``), so the lookup is deterministic.

The resolver consumes the operator-declared cluster catalogs
(:class:`OCPTargetNamespace`, :class:`TargetNetwork`,
:class:`TargetStorageClass`) to enrich resolution outputs — the NAD's
containing namespace and a StorageClass's access mode are read from
those catalogs.

This module is consumed by the planner pre-flight, the inventory
listing endpoint, the plan-creation preview, and the MTV YAML emitter.
The cap on per-VM resolution work is O(1) mapping queries per batch
via :func:`resolve_vms_bulk` — see the inventory pagination contract
in ``backend/tests/test_inventory_pagination.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ocp_namespace import OCPTargetNamespace
from app.models.target import OCPTarget, ResourceMapping
from app.models.target_network import TargetNetwork
from app.models.target_storage_class import TargetStorageClass
from app.models.vm import VM


@dataclass
class ResolvedNetwork:
    """Per-source-network resolution. ``target_network_*`` fields are
    ``None`` when the mapping doesn't cover this source network."""

    source: str
    target_network_id: int | None = None
    target_network_name: str | None = None
    target_network_namespace: str | None = None
    target_network_type: str | None = None  # nad | cudn | udn | pod

    @property
    def is_resolved(self) -> bool:
        # A "pod" target (cluster default network) doesn't need a name
        # or namespace — its presence alone is sufficient.
        if self.target_network_type == "pod":
            return True
        return bool(self.target_network_name)


@dataclass
class ResolvedStorage:
    source: str
    target_storage_class_name: str | None = None
    access_mode: str | None = None  # ReadWriteOnce | ReadWriteMany | ReadOnlyMany

    @property
    def is_resolved(self) -> bool:
        return bool(self.target_storage_class_name)


@dataclass
class ResolvedTarget:
    """Outcome of resolution for a single VM. ``is_complete`` is True
    only when cluster, namespace, every source network, and every
    source datastore have target values. ``reasons`` carries human-
    readable gap descriptions when ``is_complete`` is False."""

    cluster_id: int | None = None
    cluster_name: str | None = None
    namespace: str | None = None
    networks: list[ResolvedNetwork] = field(default_factory=list)
    storage: list[ResolvedStorage] = field(default_factory=list)
    mapping_id: int | None = None
    is_complete: bool = False
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bulk-resolver caches — populated once per resolve_vms_bulk call.
# ---------------------------------------------------------------------------
@dataclass
class _ResolverCaches:
    mappings_by_vcenter: dict[int, list[ResourceMapping]]
    mappings_by_pair: dict[tuple[int, int], ResourceMapping]
    clusters: dict[int, OCPTarget]
    networks_by_cluster: dict[int, dict[str, TargetNetwork]]
    storage_by_cluster: dict[int, dict[str, TargetStorageClass]]
    namespaces_by_cluster: dict[int, set[str]]


def _load_caches(db: Session, cluster_ids: Iterable[int] | None = None) -> _ResolverCaches:
    """Pre-load every catalog/mapping referenced by the resolver in
    bounded queries. ``cluster_ids`` narrows the cluster-scoped catalogs
    when the caller knows the relevant set upfront — passing ``None``
    is fine for small databases (test fixtures) but a 1000-VM bulk
    resolver should pre-compute the cluster set first."""
    mapping_rows: list[ResourceMapping] = list(db.execute(select(ResourceMapping)).scalars().all())
    mappings_by_vcenter: dict[int, list[ResourceMapping]] = {}
    mappings_by_pair: dict[tuple[int, int], ResourceMapping] = {}
    for m in mapping_rows:
        mappings_by_vcenter.setdefault(m.vcenter_source_id, []).append(m)
        mappings_by_pair[(m.vcenter_source_id, m.ocp_target_id)] = m

    cluster_query = select(OCPTarget)
    if cluster_ids is not None:
        cluster_query = cluster_query.where(OCPTarget.id.in_(list(cluster_ids)))
    clusters = {row.id: row for row in db.execute(cluster_query).scalars().all()}

    network_query = select(TargetNetwork)
    if cluster_ids is not None:
        network_query = network_query.where(TargetNetwork.ocp_target_id.in_(list(cluster_ids)))
    networks_by_cluster: dict[int, dict[str, TargetNetwork]] = {}
    for n in db.execute(network_query).scalars().all():
        networks_by_cluster.setdefault(n.ocp_target_id, {})[n.name] = n

    storage_query = select(TargetStorageClass)
    if cluster_ids is not None:
        storage_query = storage_query.where(TargetStorageClass.ocp_target_id.in_(list(cluster_ids)))
    storage_by_cluster: dict[int, dict[str, TargetStorageClass]] = {}
    for s in db.execute(storage_query).scalars().all():
        storage_by_cluster.setdefault(s.ocp_target_id, {})[s.name] = s

    ns_query = select(OCPTargetNamespace.ocp_target_id, OCPTargetNamespace.name)
    if cluster_ids is not None:
        ns_query = ns_query.where(OCPTargetNamespace.ocp_target_id.in_(list(cluster_ids)))
    namespaces_by_cluster: dict[int, set[str]] = {}
    for cid, name in db.execute(ns_query).all():
        namespaces_by_cluster.setdefault(cid, set()).add(name)

    return _ResolverCaches(
        mappings_by_vcenter=mappings_by_vcenter,
        mappings_by_pair=mappings_by_pair,
        clusters=clusters,
        networks_by_cluster=networks_by_cluster,
        storage_by_cluster=storage_by_cluster,
        namespaces_by_cluster=namespaces_by_cluster,
    )


def _resolve_cluster(vm: VM, caches: _ResolverCaches) -> tuple[int | None, str | None]:
    """Return (cluster_id, gap_reason). When override is set the
    override wins. Without an override, the resolver falls back to
    the sole mapping for this vCenter if exactly one exists — multiple
    mappings require an explicit override per VM since there's no
    sensible default."""
    if vm.target_cluster_id_override is not None:
        return vm.target_cluster_id_override, None

    if vm.source_vcenter_id is None:
        return None, "VM is not associated with a source vCenter"

    mappings = caches.mappings_by_vcenter.get(vm.source_vcenter_id, [])
    if not mappings:
        return None, f"No ResourceMapping exists for source vCenter {vm.source_vcenter_id}"
    if len(mappings) > 1:
        cluster_names = sorted(
            (
                caches.clusters.get(m.ocp_target_id).name
                if caches.clusters.get(m.ocp_target_id)
                else str(m.ocp_target_id)
            )
            for m in mappings
        )
        return (
            None,
            f"Multiple target clusters available for this vCenter "
            f"({', '.join(cluster_names)}); set target_cluster_id_override on the VM",
        )
    return mappings[0].ocp_target_id, None


def _resolve_namespace_via_strategy(namespace_mappings: list[dict] | dict, vm: VM) -> str | None:
    """Dispatch on the mapping's namespace_mappings shape — mirrors
    :class:`app.core.mtv.MappingResolver._resolve_strategy` so the
    inventory resolver and the YAML emitter agree."""
    if isinstance(namespace_mappings, dict):
        kind = (namespace_mappings.get("strategy") or "").lower()
        if kind == "single":
            return namespace_mappings.get("single_namespace") or None
        if kind == "per_environment":
            env = (vm.environment or "").strip().lower()
            for key, ns in (namespace_mappings.get("per_env_namespaces") or {}).items():
                if (key or "").strip().lower() == env:
                    return ns or None
            return None
        if kind == "per_application":
            prefix = namespace_mappings.get("per_app_prefix") or "app"
            app = (vm.application_hint or "").strip()
            if not app:
                return "unassigned-vms"
            slug = _slugify(app)
            return f"{prefix}-{slug}-vms"
        return None
    # Legacy criteria-row shape — first matching row wins.
    for entry in namespace_mappings or []:
        crit = entry.get("criteria") or "default"
        value = entry.get("criteria_value") or ""
        target_ns = entry.get("target_namespace") or ""
        if not target_ns:
            continue
        if crit == "default":
            return target_ns
        if crit == "environment" and (vm.environment or "") == value:
            return target_ns
        if crit == "application" and (vm.application_hint or "") == value:
            return target_ns
        if crit == "vcenter_folder" and (vm.vsphere_folder or "") == value:
            return target_ns
    return None


def _slugify(value: str) -> str:
    out: list[str] = []
    last_dash = False
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append("-")
            last_dash = True
    return "".join(out).strip("-") or "unassigned"


def _resolve_vm_with_caches(vm: VM, caches: _ResolverCaches) -> ResolvedTarget:
    result = ResolvedTarget()

    cluster_id, gap = _resolve_cluster(vm, caches)
    result.cluster_id = cluster_id
    if cluster_id is not None:
        cluster = caches.clusters.get(cluster_id)
        if cluster is not None:
            result.cluster_name = cluster.name
        else:
            result.reasons.append(
                f"target_cluster_id_override={cluster_id} is not a known OCPTarget"
            )
    if gap is not None:
        result.reasons.append(gap)
        # Without a cluster we still want to surface source resources
        # so the UI knows what needs to be mapped — fall through with
        # everything else marked unresolved.
        result.networks = [ResolvedNetwork(source=s) for s in vm.vsphere_networks or [] if s]
        result.storage = [ResolvedStorage(source=s) for s in vm.vsphere_datastores or [] if s]
        result.is_complete = False
        return result

    mapping = caches.mappings_by_pair.get(
        (vm.source_vcenter_id, cluster_id)
        if vm.source_vcenter_id is not None
        else (None, cluster_id)  # type: ignore[arg-type]
    )
    if mapping is None:
        result.reasons.append(
            f"No ResourceMapping exists for vCenter {vm.source_vcenter_id} → cluster {cluster_id}"
        )
    else:
        result.mapping_id = mapping.id

    # Namespace
    if vm.target_namespace_override:
        result.namespace = vm.target_namespace_override
    elif mapping is not None:
        result.namespace = _resolve_namespace_via_strategy(mapping.namespace_mappings or [], vm)
        if not result.namespace:
            result.reasons.append(
                "Mapping's namespace strategy did not yield a namespace for this VM"
            )
    else:
        result.reasons.append("Cannot resolve target namespace without a mapping")

    # Networks
    network_catalog = caches.networks_by_cluster.get(cluster_id, {})
    network_map_index: dict[str, dict] = {}
    if mapping is not None:
        for entry in mapping.network_mappings or []:
            src = entry.get("source_network")
            if src:
                network_map_index[src] = entry

    for src in vm.vsphere_networks or []:
        if not src:
            continue
        entry = network_map_index.get(src)
        net = ResolvedNetwork(source=src)
        if entry is not None:
            tgt_name = (entry.get("target_network_name") or "").strip()
            tgt_type = (entry.get("target_network_type") or "").strip() or None
            if tgt_name:
                net.target_network_name = tgt_name
                net.target_network_type = tgt_type
                catalog_hit = network_catalog.get(tgt_name)
                if catalog_hit is not None:
                    net.target_network_id = catalog_hit.id
                    net.target_network_namespace = catalog_hit.namespace
                    if net.target_network_type is None:
                        net.target_network_type = (
                            catalog_hit.network_type.value
                            if hasattr(catalog_hit.network_type, "value")
                            else str(catalog_hit.network_type)
                        )
                if (net.target_network_type or "").lower() == "pod":
                    net.target_network_namespace = None
                if (
                    not net.target_network_namespace
                    and (net.target_network_type or "").lower() != "pod"
                ):
                    # Default NAD namespace heuristic — the operator
                    # declared NAD lives in the workload namespace if
                    # the catalog row left it empty. Mark it as a gap
                    # rather than fabricating one.
                    result.reasons.append(
                        f"target network {tgt_name!r} has no declared namespace in the cluster catalog"
                    )
            elif (tgt_type or "").lower() == "pod":
                net.target_network_type = "pod"
        if not net.is_resolved and mapping is not None:
            result.reasons.append(f"source network {src!r} is not mapped")
        result.networks.append(net)

    # Storage
    storage_catalog = caches.storage_by_cluster.get(cluster_id, {})
    storage_map_index: dict[str, dict] = {}
    if mapping is not None:
        for entry in mapping.storage_mappings or []:
            src = entry.get("source_datastore")
            if src:
                storage_map_index[src] = entry

    for src in vm.vsphere_datastores or []:
        if not src:
            continue
        entry = storage_map_index.get(src)
        stor = ResolvedStorage(source=src)
        if entry is not None:
            tgt_sc = (entry.get("target_storage_class") or "").strip()
            if tgt_sc:
                stor.target_storage_class_name = tgt_sc
                catalog_hit = storage_catalog.get(tgt_sc)
                if catalog_hit is not None:
                    stor.access_mode = (
                        catalog_hit.access_mode.value
                        if hasattr(catalog_hit.access_mode, "value")
                        else str(catalog_hit.access_mode)
                    )
                elif entry.get("access_mode"):
                    stor.access_mode = entry["access_mode"]
        if not stor.is_resolved and mapping is not None:
            result.reasons.append(f"source datastore {src!r} is not mapped")
        result.storage.append(stor)

    networks_complete = all(n.is_resolved for n in result.networks)
    storage_complete = all(s.is_resolved for s in result.storage)
    result.is_complete = (
        result.cluster_id is not None
        and bool(result.namespace)
        and networks_complete
        and storage_complete
        and mapping is not None
    )
    return result


def resolve_vm_target(vm: VM, db: Session) -> ResolvedTarget:
    """Resolve a single VM's target. Pre-loads only the catalogs for
    the VM's resolved cluster; callers resolving many VMs at once
    should use :func:`resolve_vms_bulk` instead."""
    # First pass uses the mapping caches to determine cluster; then we
    # narrow the catalogs. For one-shot resolution the overhead is the
    # same as loading everything (single DB session anyway).
    caches = _load_caches(db)
    return _resolve_vm_with_caches(vm, caches)


def resolve_vms_bulk(vm_ids: list[int], db: Session) -> dict[int, ResolvedTarget]:
    """Resolve many VMs in O(1) mapping/catalog queries.

    Returns a dict keyed by VM id. Missing IDs are silently skipped —
    callers wrap with their own 404 handling.
    """
    if not vm_ids:
        return {}
    vms = list(db.execute(select(VM).where(VM.id.in_(vm_ids))).scalars().all())
    caches = _load_caches(db)
    return {vm.id: _resolve_vm_with_caches(vm, caches) for vm in vms}


def resolve_vms_iter(vms: Iterable[VM], db: Session) -> dict[int, ResolvedTarget]:
    """Same as :func:`resolve_vms_bulk` but takes the loaded VM rows
    directly — useful when the caller already has a SQLAlchemy result
    set in hand (the inventory list endpoint, for instance)."""
    caches = _load_caches(db)
    return {vm.id: _resolve_vm_with_caches(vm, caches) for vm in vms}
