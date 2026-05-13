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
from app.models.target import OCPTarget, ResourceMapping
from app.models.target_network import TargetNetwork, TargetNetworkType
from app.models.target_storage_class import (
    StorageAccessMode,
    TargetStorageClass,
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
