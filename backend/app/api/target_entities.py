"""CRUD endpoints for operator-defined TargetNetwork / TargetStorageClass.

These rows are what the mapping editor's target dropdowns pull from.
Live cluster discovery is not authoritative; the operator declares
which NetworkAttachmentDefinitions / CUDNs / UDNs and StorageClasses
exist on the cluster, then maps source vSphere resources onto them.

DELETE returns 409 with a list of referencing ResourceMapping rows
when the entity is still in use. The operator clears the mapping
first, then retries the delete.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import get_db
from app.models.ocp_namespace import OCPTargetNamespace
from app.models.target import OCPTarget, ResourceMapping
from app.models.target_network import TargetNetwork, TargetNetworkType
from app.models.target_storage_class import (
    StorageAccessMode,
    TargetStorageClass,
)
from app.models.vm import VM
from app.schemas.ocp_namespace import (
    OCPTargetNamespaceCreate,
    OCPTargetNamespaceListResponse,
    OCPTargetNamespaceRead,
    OCPTargetNamespaceUpdate,
)
from app.schemas.target_entities import (
    TargetNetworkCreate,
    TargetNetworkRead,
    TargetNetworkUpdate,
    TargetStorageClassCreate,
    TargetStorageClassRead,
    TargetStorageClassUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["target-entities"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_target_or_404(db: Session, target_id: int) -> OCPTarget:
    target = db.get(OCPTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail=f"OCP target {target_id} not found")
    return target


def _enforce_single_network_default(
    db: Session, target_id: int, network_type: TargetNetworkType, skip_id: int | None
) -> None:
    """At most one default per (cluster, network_type). The UI surfaces
    the default flag per-type because operators commonly want a default
    NAD AND a default CUDN — they aren't competing for the same slot."""
    q = select(TargetNetwork).where(
        TargetNetwork.ocp_target_id == target_id,
        TargetNetwork.network_type == network_type,
        TargetNetwork.is_default.is_(True),
    )
    if skip_id is not None:
        q = q.where(TargetNetwork.id != skip_id)
    for other in db.scalars(q).all():
        other.is_default = False


def _enforce_single_storage_default(db: Session, target_id: int, skip_id: int | None) -> None:
    """At most one default StorageClass per cluster (across access modes)."""
    q = select(TargetStorageClass).where(
        TargetStorageClass.ocp_target_id == target_id,
        TargetStorageClass.is_default.is_(True),
    )
    if skip_id is not None:
        q = q.where(TargetStorageClass.id != skip_id)
    for other in db.scalars(q).all():
        other.is_default = False


def _mappings_referencing_network(
    db: Session, target_id: int, network_name: str
) -> list[ResourceMapping]:
    """Return ResourceMappings on the same OCP target whose
    ``network_mappings`` JSON list references ``network_name``.

    We scan in Python rather than crafting a portable JSONB query —
    the per-target row count is small (one mapping per
    source/target pair, typically <10) so a scan is cheap and works
    on SQLite too."""
    mappings = db.scalars(
        select(ResourceMapping).where(ResourceMapping.ocp_target_id == target_id)
    ).all()
    out: list[ResourceMapping] = []
    for m in mappings:
        for row in m.network_mappings or []:
            if (row or {}).get("target_network_name") == network_name:
                out.append(m)
                break
    return out


def _mappings_referencing_sc(db: Session, target_id: int, sc_name: str) -> list[ResourceMapping]:
    mappings = db.scalars(
        select(ResourceMapping).where(ResourceMapping.ocp_target_id == target_id)
    ).all()
    out: list[ResourceMapping] = []
    for m in mappings:
        for row in m.storage_mappings or []:
            if (row or {}).get("target_storage_class") == sc_name:
                out.append(m)
                break
    return out


# ---------------------------------------------------------------------------
# TargetNetwork CRUD
# ---------------------------------------------------------------------------
@router.get("/{target_id}/networks", response_model=list[TargetNetworkRead])
def list_networks(target_id: int, db: Session = Depends(get_db)) -> list[TargetNetwork]:
    _get_target_or_404(db, target_id)
    return list(
        db.scalars(
            select(TargetNetwork)
            .where(TargetNetwork.ocp_target_id == target_id)
            .order_by(TargetNetwork.name)
        ).all()
    )


@router.post(
    "/{target_id}/networks",
    response_model=TargetNetworkRead,
    status_code=status.HTTP_201_CREATED,
)
def create_network(
    request: Request,
    target_id: int,
    payload: TargetNetworkCreate,
    db: Session = Depends(get_db),
) -> TargetNetwork:
    _get_target_or_404(db, target_id)
    net = TargetNetwork(
        ocp_target_id=target_id,
        name=payload.name,
        network_type=TargetNetworkType(payload.network_type),
        namespace=payload.namespace,
        is_default=payload.is_default,
        notes=payload.notes,
    )
    db.add(net)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(f"Network named {payload.name!r} already exists on this cluster"),
        ) from e
    if net.is_default:
        _enforce_single_network_default(db, target_id, net.network_type, skip_id=net.id)
    db.commit()
    db.refresh(net)
    record_audit(
        db,
        action="target_network.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="target_network",
        resource_id=net.id,
        details={
            "ocp_target_id": target_id,
            "name": net.name,
            "type": net.network_type.value,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return net


@router.get("/{target_id}/networks/{net_id}", response_model=TargetNetworkRead)
def get_network(target_id: int, net_id: int, db: Session = Depends(get_db)) -> TargetNetwork:
    net = db.get(TargetNetwork, net_id)
    if net is None or net.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"Target network {net_id} not found on cluster {target_id}",
        )
    return net


@router.patch("/{target_id}/networks/{net_id}", response_model=TargetNetworkRead)
def update_network(
    request: Request,
    target_id: int,
    net_id: int,
    payload: TargetNetworkUpdate,
    db: Session = Depends(get_db),
) -> TargetNetwork:
    net = db.get(TargetNetwork, net_id)
    if net is None or net.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"Target network {net_id} not found on cluster {target_id}",
        )
    updates = payload.model_dump(exclude_unset=True)
    if "network_type" in updates and updates["network_type"] is not None:
        net.network_type = TargetNetworkType(updates["network_type"])
    for field in ("name", "namespace", "is_default", "notes"):
        if field in updates and updates[field] is not None:
            setattr(net, field, updates[field])
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(f"Network named {net.name!r} already exists on this cluster"),
        ) from e
    if net.is_default:
        _enforce_single_network_default(db, target_id, net.network_type, skip_id=net.id)
    db.commit()
    db.refresh(net)
    request.state.skip_audit_log = True
    return net


@router.delete(
    "/{target_id}/networks/{net_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_network(
    request: Request,
    target_id: int,
    net_id: int,
    db: Session = Depends(get_db),
) -> None:
    net = db.get(TargetNetwork, net_id)
    if net is None or net.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"Target network {net_id} not found on cluster {target_id}",
        )
    referencing = _mappings_referencing_network(db, target_id, net.name)
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": (
                    f"Network {net.name!r} is still referenced by "
                    f"{len(referencing)} resource mapping(s); unmap before deleting."
                ),
                "referenced_by": [
                    {"mapping_id": m.id, "mapping_name": m.name} for m in referencing
                ],
            },
        )
    record_audit(
        db,
        action="target_network.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="target_network",
        resource_id=net.id,
        details={"ocp_target_id": target_id, "name": net.name},
    )
    db.delete(net)
    db.commit()
    request.state.skip_audit_log = True


# ---------------------------------------------------------------------------
# TargetStorageClass CRUD
# ---------------------------------------------------------------------------
@router.get(
    "/{target_id}/storage-classes",
    response_model=list[TargetStorageClassRead],
)
def list_storage_classes(target_id: int, db: Session = Depends(get_db)) -> list[TargetStorageClass]:
    _get_target_or_404(db, target_id)
    return list(
        db.scalars(
            select(TargetStorageClass)
            .where(TargetStorageClass.ocp_target_id == target_id)
            .order_by(TargetStorageClass.name)
        ).all()
    )


@router.post(
    "/{target_id}/storage-classes",
    response_model=TargetStorageClassRead,
    status_code=status.HTTP_201_CREATED,
)
def create_storage_class(
    request: Request,
    target_id: int,
    payload: TargetStorageClassCreate,
    db: Session = Depends(get_db),
) -> TargetStorageClass:
    _get_target_or_404(db, target_id)
    sc = TargetStorageClass(
        ocp_target_id=target_id,
        name=payload.name,
        access_mode=StorageAccessMode(payload.access_mode),
        is_default=payload.is_default,
        notes=payload.notes,
    )
    db.add(sc)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(f"StorageClass named {payload.name!r} already exists on this cluster"),
        ) from e
    if sc.is_default:
        _enforce_single_storage_default(db, target_id, skip_id=sc.id)
    db.commit()
    db.refresh(sc)
    record_audit(
        db,
        action="target_storage_class.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="target_storage_class",
        resource_id=sc.id,
        details={
            "ocp_target_id": target_id,
            "name": sc.name,
            "access_mode": sc.access_mode.value,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return sc


@router.get(
    "/{target_id}/storage-classes/{sc_id}",
    response_model=TargetStorageClassRead,
)
def get_storage_class(
    target_id: int, sc_id: int, db: Session = Depends(get_db)
) -> TargetStorageClass:
    sc = db.get(TargetStorageClass, sc_id)
    if sc is None or sc.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"StorageClass {sc_id} not found on cluster {target_id}",
        )
    return sc


@router.patch(
    "/{target_id}/storage-classes/{sc_id}",
    response_model=TargetStorageClassRead,
)
def update_storage_class(
    request: Request,
    target_id: int,
    sc_id: int,
    payload: TargetStorageClassUpdate,
    db: Session = Depends(get_db),
) -> TargetStorageClass:
    sc = db.get(TargetStorageClass, sc_id)
    if sc is None or sc.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"StorageClass {sc_id} not found on cluster {target_id}",
        )
    updates = payload.model_dump(exclude_unset=True)
    if "access_mode" in updates and updates["access_mode"] is not None:
        sc.access_mode = StorageAccessMode(updates["access_mode"])
    for field in ("name", "is_default", "notes"):
        if field in updates and updates[field] is not None:
            setattr(sc, field, updates[field])
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(f"StorageClass named {sc.name!r} already exists on this cluster"),
        ) from e
    if sc.is_default:
        _enforce_single_storage_default(db, target_id, skip_id=sc.id)
    db.commit()
    db.refresh(sc)
    request.state.skip_audit_log = True
    return sc


@router.delete(
    "/{target_id}/storage-classes/{sc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_storage_class(
    request: Request,
    target_id: int,
    sc_id: int,
    db: Session = Depends(get_db),
) -> None:
    sc = db.get(TargetStorageClass, sc_id)
    if sc is None or sc.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"StorageClass {sc_id} not found on cluster {target_id}",
        )
    referencing = _mappings_referencing_sc(db, target_id, sc.name)
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": (
                    f"StorageClass {sc.name!r} is still referenced by "
                    f"{len(referencing)} resource mapping(s); unmap before deleting."
                ),
                "referenced_by": [
                    {"mapping_id": m.id, "mapping_name": m.name} for m in referencing
                ],
            },
        )
    record_audit(
        db,
        action="target_storage_class.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="target_storage_class",
        resource_id=sc.id,
        details={"ocp_target_id": target_id, "name": sc.name},
    )
    db.delete(sc)
    db.commit()
    request.state.skip_audit_log = True


# ---------------------------------------------------------------------------
# OCPTargetNamespace CRUD
# ---------------------------------------------------------------------------
# Operator-declared catalog of target namespaces per cluster. Plan
# generation (target resolution) uses these to validate that a VM's
# resolved namespace is actually known to the cluster. Live discovery
# is intentionally absent — the operator declares ground truth, the
# appliance does not authenticate to clusters.
def _namespace_referencing_vms(db: Session, target_id: int, namespace_name: str) -> list[VM]:
    """VMs whose target_cluster_id_override + target_namespace_override
    pin them to this namespace on this cluster. Used by the catalog
    delete endpoint to block removal while the namespace is in use."""
    return list(
        db.scalars(
            select(VM).where(
                VM.target_cluster_id_override == target_id,
                VM.target_namespace_override == namespace_name,
            )
        ).all()
    )


def _namespace_referencing_mappings(
    db: Session, target_id: int, namespace_name: str
) -> list[ResourceMapping]:
    """ResourceMappings whose namespace_mappings JSON references this
    namespace name. Walks both the legacy criteria-row shape and the
    NamespaceStrategy dict shape."""
    mappings = db.scalars(
        select(ResourceMapping).where(ResourceMapping.ocp_target_id == target_id)
    ).all()
    out: list[ResourceMapping] = []
    for m in mappings:
        nm = m.namespace_mappings or []
        if isinstance(nm, dict):
            if (nm.get("single_namespace") or "") == namespace_name:
                out.append(m)
                continue
            if namespace_name in (nm.get("per_env_namespaces") or {}).values():
                out.append(m)
                continue
        else:
            for row in nm:
                if (row or {}).get("target_namespace") == namespace_name:
                    out.append(m)
                    break
    return out


@router.get(
    "/{target_id}/namespaces",
    response_model=OCPTargetNamespaceListResponse,
)
def list_namespaces(
    target_id: int,
    skip: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
) -> dict:
    _get_target_or_404(db, target_id)
    base = select(OCPTargetNamespace).where(OCPTargetNamespace.ocp_target_id == target_id)
    total = db.scalar(select(_count_star()).select_from(base.subquery())) or 0
    items = list(db.scalars(base.order_by(OCPTargetNamespace.name).offset(skip).limit(limit)).all())
    return {"items": items, "total": int(total), "skip": skip, "limit": limit}


@router.post(
    "/{target_id}/namespaces",
    response_model=OCPTargetNamespaceRead,
    status_code=status.HTTP_201_CREATED,
)
def create_namespace(
    request: Request,
    target_id: int,
    payload: OCPTargetNamespaceCreate,
    db: Session = Depends(get_db),
) -> OCPTargetNamespace:
    _get_target_or_404(db, target_id)
    ns = OCPTargetNamespace(
        ocp_target_id=target_id,
        name=payload.name,
        description=payload.description,
    )
    db.add(ns)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Namespace named {payload.name!r} already exists on this cluster",
        ) from e
    db.commit()
    db.refresh(ns)
    record_audit(
        db,
        action="ocp_target_namespace.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="ocp_target_namespace",
        resource_id=ns.id,
        details={"ocp_target_id": target_id, "name": ns.name},
    )
    db.commit()
    request.state.skip_audit_log = True
    return ns


@router.get(
    "/{target_id}/namespaces/{ns_id}",
    response_model=OCPTargetNamespaceRead,
)
def get_namespace(target_id: int, ns_id: int, db: Session = Depends(get_db)) -> OCPTargetNamespace:
    ns = db.get(OCPTargetNamespace, ns_id)
    if ns is None or ns.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"Namespace {ns_id} not found on cluster {target_id}",
        )
    return ns


@router.patch(
    "/{target_id}/namespaces/{ns_id}",
    response_model=OCPTargetNamespaceRead,
)
def update_namespace(
    request: Request,
    target_id: int,
    ns_id: int,
    payload: OCPTargetNamespaceUpdate,
    db: Session = Depends(get_db),
) -> OCPTargetNamespace:
    ns = db.get(OCPTargetNamespace, ns_id)
    if ns is None or ns.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"Namespace {ns_id} not found on cluster {target_id}",
        )
    updates = payload.model_dump(exclude_unset=True)
    for field in ("name", "description"):
        if field in updates and updates[field] is not None:
            setattr(ns, field, updates[field])
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Namespace named {ns.name!r} already exists on this cluster",
        ) from e
    db.commit()
    db.refresh(ns)
    request.state.skip_audit_log = True
    return ns


@router.delete(
    "/{target_id}/namespaces/{ns_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_namespace(
    request: Request,
    target_id: int,
    ns_id: int,
    db: Session = Depends(get_db),
) -> None:
    ns = db.get(OCPTargetNamespace, ns_id)
    if ns is None or ns.ocp_target_id != target_id:
        raise HTTPException(
            status_code=404,
            detail=f"Namespace {ns_id} not found on cluster {target_id}",
        )
    referencing_vms = _namespace_referencing_vms(db, target_id, ns.name)
    referencing_mappings = _namespace_referencing_mappings(db, target_id, ns.name)
    if referencing_vms or referencing_mappings:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": (
                    f"Namespace {ns.name!r} is still referenced by "
                    f"{len(referencing_vms)} VM(s) and "
                    f"{len(referencing_mappings)} mapping(s); clear the "
                    "references before deleting."
                ),
                "referenced_by": (
                    [{"type": "vm", "id": v.id, "name": v.name} for v in referencing_vms]
                    + [
                        {"type": "mapping", "id": m.id, "name": m.name}
                        for m in referencing_mappings
                    ]
                ),
            },
        )
    record_audit(
        db,
        action="ocp_target_namespace.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="ocp_target_namespace",
        resource_id=ns.id,
        details={"ocp_target_id": target_id, "name": ns.name},
    )
    db.delete(ns)
    db.commit()
    request.state.skip_audit_log = True


def _count_star():
    """Tiny shim around sqlalchemy.func.count() so the import list stays clean."""
    from sqlalchemy import func

    return func.count()
