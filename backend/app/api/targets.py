"""OCP target cluster registry + ResourceMapping CRUD + plan-time mapping resolution.

Three logical groupings live in this file because they all share the
"plan generation needs real cluster resources, not placeholders" thread:

  - ``/api/sources/targets`` — register OCP target clusters, run
    discovery, fetch cached resources.
  - ``/api/mappings`` — CRUD for ResourceMapping rows, with LLM-driven
    suggestion endpoints + a pre-flight check.
  - Helpers consumed by ``app.core.plan_generation`` so plan generation
    can validate "every VM in scope has a network and datastore that's
    mapped" before calling the LLM.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.core.db import get_db
from app.core.mapping_suggester import (
    SuggestionError,
    suggest_network_mappings,
    suggest_storage_mappings,
)
from app.core.ocp_discovery import OCPDiscoveryClient, OCPDiscoveryError
from app.models.target import (
    OCPAuthType,
    OCPTarget,
    OCPTargetStatus,
    ResourceMapping,
    ResourceMappingStatus,
)
from app.models.vcenter import VCenterSource
from app.models.vm import VM
from app.schemas.target import (
    MappingSuggestionResponse,
    OCPDiscoveryRequest,
    OCPDiscoveryResponse,
    OCPTargetCreate,
    OCPTargetRead,
    OCPTargetUpdate,
    PreflightCheckResponse,
    ResourceMappingCreate,
    ResourceMappingRead,
    ResourceMappingUpdate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Targets router
# ---------------------------------------------------------------------------
targets_router = APIRouter(tags=["ocp-targets"])


def _get_target_or_404(db: Session, target_id: int) -> OCPTarget:
    target = db.get(OCPTarget, target_id)
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"OCP target {target_id} not found"
        )
    return target


@targets_router.get("", response_model=list[OCPTargetRead])
def list_targets(db: Session = Depends(get_db)) -> list[OCPTarget]:
    return list(
        db.scalars(select(OCPTarget).order_by(OCPTarget.name)).all()
    )


@targets_router.post(
    "", response_model=OCPTargetRead, status_code=status.HTTP_201_CREATED
)
def create_target(
    request: Request,
    payload: OCPTargetCreate,
    db: Session = Depends(get_db),
) -> OCPTarget:
    target = OCPTarget(**payload.model_dump())
    db.add(target)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"OCP target named {payload.name!r} already exists",
        ) from e
    db.refresh(target)
    record_audit(
        db,
        action="ocp_target.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="ocp_target",
        resource_id=target.id,
        details={
            "name": target.name,
            "endpoint": target.api_endpoint,
            "classification_level": target.classification_level.value,
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return target


@targets_router.get("/{target_id}", response_model=OCPTargetRead)
def get_target(target_id: int, db: Session = Depends(get_db)) -> OCPTarget:
    return _get_target_or_404(db, target_id)


@targets_router.patch("/{target_id}", response_model=OCPTargetRead)
def update_target(
    request: Request,
    target_id: int,
    payload: OCPTargetUpdate,
    db: Session = Depends(get_db),
) -> OCPTarget:
    target = _get_target_or_404(db, target_id)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(target, field, value)
    db.commit()
    db.refresh(target)
    request.state.skip_audit_log = True
    return target


@targets_router.delete("/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_target(
    request: Request,
    target_id: int,
    db: Session = Depends(get_db),
) -> None:
    target = _get_target_or_404(db, target_id)
    record_audit(
        db,
        action="ocp_target.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="ocp_target",
        resource_id=target.id,
        details={"name": target.name},
    )
    db.delete(target)
    db.commit()
    request.state.skip_audit_log = True


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------
@targets_router.post(
    "/{target_id}/discover", response_model=OCPDiscoveryResponse
)
def discover_target(
    request: Request,
    target_id: int,
    payload: OCPDiscoveryRequest | None = None,
    db: Session = Depends(get_db),
) -> dict:
    """Run discovery against the target cluster.

    Two paths:

    1. **Live discovery** — pass ``bearer_token`` in the request and
       the appliance queries the K8s API directly. Common for
       in-network operators with cluster credentials.

    2. **Manual discovery** — pass ``manual_storage_classes`` /
       ``manual_network_attachments`` / ``manual_namespaces`` from a
       prior ``oc get -o json`` run. Air-gapped operators use this
       when the appliance can't reach the target cluster directly.

    Either way the result lands on the OCPTarget row's discovery
    columns, marking ``last_synced_at`` and clearing ``last_error``.
    """
    target = _get_target_or_404(db, target_id)
    actor = request.headers.get("x-actor", "user")
    payload = payload or OCPDiscoveryRequest()

    if payload.manual_storage_classes is not None or payload.manual_network_attachments is not None or payload.manual_namespaces is not None:
        # Manual discovery — operator pasted resources. Trust the
        # input shape (Pydantic already validated it) and write straight
        # through.
        target.storage_classes = (
            [s.model_dump() for s in (payload.manual_storage_classes or [])]
        )
        target.network_attachments = (
            [n.model_dump() for n in (payload.manual_network_attachments or [])]
        )
        target.namespaces = (
            [ns.model_dump() for ns in (payload.manual_namespaces or [])]
        )
        target.cluster_capacity = target.cluster_capacity or {}
        target.last_synced_at = datetime.now(timezone.utc)
        target.last_error = None
        target.status = OCPTargetStatus.active
    else:
        # Live discovery. Bearer token is required; we don't fall
        # back to anonymous access.
        if not payload.bearer_token:
            raise HTTPException(
                status_code=422,
                detail="Provide bearer_token for live discovery, or pass manual_* fields for offline discovery.",
            )
        client = OCPDiscoveryClient(
            api_endpoint=target.api_endpoint,
            bearer_token=payload.bearer_token,
            verify_ssl=target.verify_ssl,
        )
        try:
            result = client.discover_all()
        except OCPDiscoveryError as e:
            target.status = OCPTargetStatus.error
            target.last_error = str(e)
            db.commit()
            db.refresh(target)
            record_audit(
                db,
                action="ocp_target.discovery_failed",
                actor=actor,
                resource_type="ocp_target",
                resource_id=target.id,
                details={"error": str(e), "endpoint": e.endpoint},
            )
            db.commit()
            raise HTTPException(status_code=502, detail=str(e)) from e
        target.storage_classes = result.storage_classes
        target.network_attachments = result.network_attachments
        target.namespaces = result.namespaces
        target.cluster_capacity = result.cluster_capacity
        target.mtv_namespace = result.mtv_namespace
        target.ocp_version = result.ocp_version
        target.kubernetes_version = result.kubernetes_version
        target.last_synced_at = datetime.now(timezone.utc)
        target.last_error = None
        target.status = OCPTargetStatus.active

    db.commit()
    db.refresh(target)
    record_audit(
        db,
        action="ocp_target.discovered",
        actor=actor,
        resource_type="ocp_target",
        resource_id=target.id,
        details={
            "storage_classes": len(target.storage_classes or []),
            "network_attachments": len(target.network_attachments or []),
            "namespaces": len(target.namespaces or []),
        },
    )
    db.commit()
    request.state.skip_audit_log = True

    return {
        "target_id": target.id,
        "status": target.status,
        "last_synced_at": target.last_synced_at,
        "storage_class_count": len(target.storage_classes or []),
        "network_attachment_count": len(target.network_attachments or []),
        "namespace_count": len(target.namespaces or []),
        "last_error": target.last_error,
    }


# ---------------------------------------------------------------------------
# Mappings router
# ---------------------------------------------------------------------------
mappings_router = APIRouter(tags=["resource-mappings"])


def _get_mapping_or_404(db: Session, mapping_id: int) -> ResourceMapping:
    mapping = db.get(ResourceMapping, mapping_id)
    if mapping is None:
        raise HTTPException(
            status_code=404, detail=f"Resource mapping {mapping_id} not found"
        )
    return mapping


def _compute_mapping_status(
    mapping: ResourceMapping, db: Session
) -> ResourceMappingStatus:
    """Walk the mapping payload + the cluster scope's source resources
    to determine completeness.

    ``complete``: every distinct source network + datastore in the
    vCenter scope has a non-null target.

    ``incomplete``: at least one source resource has no target
    selected.

    ``needs_review``: target row references a resource that's no
    longer present on the cluster (drift).
    """
    networks = {m.get("source_network") for m in (mapping.network_mappings or [])
                if (m.get("target_network_name") or "").strip()}
    datastores = {m.get("source_datastore") for m in (mapping.storage_mappings or [])
                  if (m.get("target_storage_class") or "").strip()}

    vms = list(
        db.scalars(
            select(VM).where(VM.source_vcenter_id == mapping.vcenter_source_id)
        ).all()
    )
    needed_networks: set[str] = set()
    needed_datastores: set[str] = set()
    for vm in vms:
        for n in vm.vsphere_networks or []:
            needed_networks.add(n)
        for d in vm.vsphere_datastores or []:
            needed_datastores.add(d)

    if not needed_networks.issubset(networks):
        return ResourceMappingStatus.incomplete
    if not needed_datastores.issubset(datastores):
        return ResourceMappingStatus.incomplete

    target = db.get(OCPTarget, mapping.ocp_target_id)
    if target is not None and target.storage_classes is not None:
        target_sc_names = {sc.get("name") for sc in target.storage_classes}
        for m in mapping.storage_mappings or []:
            referenced = m.get("target_storage_class")
            if referenced and referenced not in target_sc_names:
                return ResourceMappingStatus.needs_review
        target_net_names = {
            n.get("name") for n in (target.network_attachments or [])
        }
        for m in mapping.network_mappings or []:
            referenced = m.get("target_network_name")
            if referenced and referenced not in target_net_names:
                return ResourceMappingStatus.needs_review

    return ResourceMappingStatus.complete


@mappings_router.get("", response_model=list[ResourceMappingRead])
def list_mappings(db: Session = Depends(get_db)) -> list[ResourceMapping]:
    return list(
        db.scalars(
            select(ResourceMapping).order_by(ResourceMapping.updated_at.desc())
        ).all()
    )


@mappings_router.post(
    "", response_model=ResourceMappingRead, status_code=status.HTTP_201_CREATED
)
def create_mapping(
    request: Request,
    payload: ResourceMappingCreate,
    db: Session = Depends(get_db),
) -> ResourceMapping:
    if db.get(VCenterSource, payload.vcenter_source_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"vCenter source {payload.vcenter_source_id} not found",
        )
    if db.get(OCPTarget, payload.ocp_target_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"OCP target {payload.ocp_target_id} not found",
        )

    mapping = ResourceMapping(
        name=payload.name,
        vcenter_source_id=payload.vcenter_source_id,
        ocp_target_id=payload.ocp_target_id,
        network_mappings=[m.model_dump() for m in payload.network_mappings],
        storage_mappings=[m.model_dump() for m in payload.storage_mappings],
        namespace_mappings=[m.model_dump() for m in payload.namespace_mappings],
        is_active=payload.is_active,
    )
    db.add(mapping)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                f"Mapping named {payload.name!r} already exists for this "
                "source/target pair"
            ),
        ) from e
    mapping.status = _compute_mapping_status(mapping, db)
    if payload.is_active:
        # Single-active per (source, target) pair — flip every other
        # mapping for this pair off.
        db.query(ResourceMapping).filter(
            ResourceMapping.vcenter_source_id == mapping.vcenter_source_id,
            ResourceMapping.ocp_target_id == mapping.ocp_target_id,
            ResourceMapping.id != mapping.id,
        ).update({ResourceMapping.is_active: False}, synchronize_session=False)
    db.commit()
    db.refresh(mapping)
    record_audit(
        db,
        action="resource_mapping.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="resource_mapping",
        resource_id=mapping.id,
        details={
            "name": mapping.name,
            "source": mapping.vcenter_source_id,
            "target": mapping.ocp_target_id,
            "network_rows": len(mapping.network_mappings or []),
            "storage_rows": len(mapping.storage_mappings or []),
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return mapping


@mappings_router.get("/{mapping_id}", response_model=ResourceMappingRead)
def get_mapping(mapping_id: int, db: Session = Depends(get_db)) -> ResourceMapping:
    mapping = _get_mapping_or_404(db, mapping_id)
    # Recompute status on read so drift surfaces without an explicit refresh.
    mapping.status = _compute_mapping_status(mapping, db)
    db.commit()
    db.refresh(mapping)
    return mapping


@mappings_router.patch(
    "/{mapping_id}", response_model=ResourceMappingRead
)
def update_mapping(
    request: Request,
    mapping_id: int,
    payload: ResourceMappingUpdate,
    db: Session = Depends(get_db),
) -> ResourceMapping:
    mapping = _get_mapping_or_404(db, mapping_id)
    updates = payload.model_dump(exclude_unset=True)
    if "network_mappings" in updates and updates["network_mappings"] is not None:
        mapping.network_mappings = [m for m in updates["network_mappings"]]
    if "storage_mappings" in updates and updates["storage_mappings"] is not None:
        mapping.storage_mappings = [m for m in updates["storage_mappings"]]
    if "namespace_mappings" in updates and updates["namespace_mappings"] is not None:
        mapping.namespace_mappings = [m for m in updates["namespace_mappings"]]
    if "name" in updates and updates["name"] is not None:
        mapping.name = updates["name"]
    if "is_active" in updates and updates["is_active"] is not None:
        mapping.is_active = updates["is_active"]
        if mapping.is_active:
            db.query(ResourceMapping).filter(
                ResourceMapping.vcenter_source_id == mapping.vcenter_source_id,
                ResourceMapping.ocp_target_id == mapping.ocp_target_id,
                ResourceMapping.id != mapping.id,
            ).update(
                {ResourceMapping.is_active: False}, synchronize_session=False
            )
    mapping.status = _compute_mapping_status(mapping, db)
    db.commit()
    db.refresh(mapping)
    request.state.skip_audit_log = True
    return mapping


@mappings_router.delete(
    "/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_mapping(
    request: Request,
    mapping_id: int,
    db: Session = Depends(get_db),
) -> None:
    mapping = _get_mapping_or_404(db, mapping_id)
    record_audit(
        db,
        action="resource_mapping.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="resource_mapping",
        resource_id=mapping.id,
        details={"name": mapping.name},
    )
    db.delete(mapping)
    db.commit()
    request.state.skip_audit_log = True


# ---------------------------------------------------------------------------
# LLM suggestions
# ---------------------------------------------------------------------------
def _vcenter_source_signals(
    db: Session, mapping: ResourceMapping
) -> tuple[list[dict], list[dict]]:
    """Aggregate source networks/datastores in the mapping's vCenter
    scope. Returns lists shaped for the suggestion prompts."""
    vms = list(
        db.scalars(
            select(VM).where(VM.source_vcenter_id == mapping.vcenter_source_id)
        ).all()
    )
    net_counts: Counter[str] = Counter()
    ds_counts: Counter[str] = Counter()
    for vm in vms:
        for n in vm.vsphere_networks or []:
            net_counts[n] += 1
        for d in vm.vsphere_datastores or []:
            ds_counts[d] += 1
    networks = [
        {"name": name, "vm_count": count}
        for name, count in sorted(net_counts.items(), key=lambda x: -x[1])
    ]
    datastores = [
        {"name": name, "vm_count": count}
        for name, count in sorted(ds_counts.items(), key=lambda x: -x[1])
    ]
    return networks, datastores


@mappings_router.post(
    "/{mapping_id}/suggest-network", response_model=MappingSuggestionResponse
)
def suggest_networks(
    mapping_id: int, db: Session = Depends(get_db)
) -> dict:
    mapping = _get_mapping_or_404(db, mapping_id)
    target = db.get(OCPTarget, mapping.ocp_target_id)
    if target is None or not target.network_attachments:
        raise HTTPException(
            status_code=409,
            detail="Run discovery on the target cluster before requesting suggestions.",
        )
    networks, _ = _vcenter_source_signals(db, mapping)
    target_payload = [
        {
            "name": n.get("name"),
            "type": n.get("type"),
            "namespace": n.get("namespace"),
        }
        for n in target.network_attachments
    ]
    try:
        result = suggest_network_mappings(sources=networks, targets=target_payload)
    except SuggestionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return result


@mappings_router.post(
    "/{mapping_id}/suggest-storage", response_model=MappingSuggestionResponse
)
def suggest_storage(
    mapping_id: int, db: Session = Depends(get_db)
) -> dict:
    mapping = _get_mapping_or_404(db, mapping_id)
    target = db.get(OCPTarget, mapping.ocp_target_id)
    if target is None or not target.storage_classes:
        raise HTTPException(
            status_code=409,
            detail="Run discovery on the target cluster before requesting suggestions.",
        )
    _, datastores = _vcenter_source_signals(db, mapping)
    target_payload = [
        {
            "name": s.get("name"),
            "provisioner": s.get("provisioner"),
            "is_default": s.get("is_default"),
            "access_modes": s.get("access_modes") or [],
        }
        for s in target.storage_classes
    ]
    try:
        result = suggest_storage_mappings(sources=datastores, targets=target_payload)
    except SuggestionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return result


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------
@mappings_router.post(
    "/{mapping_id}/preflight", response_model=PreflightCheckResponse
)
def preflight(mapping_id: int, db: Session = Depends(get_db)) -> dict:
    """Validate a mapping is ready to drive plan generation.

    Reports four classes of failure:
      - Source resources without a mapping entry (unmapped_*)
      - Target resources referenced by the mapping but missing on
        the cluster (missing_*_on_target — drift)
      - Plus general warnings (target inactive, mapping flagged
        needs_review, no namespace mapping configured, etc.)

    The endpoint never modifies the mapping; it's read-only.
    """
    mapping = _get_mapping_or_404(db, mapping_id)
    target = db.get(OCPTarget, mapping.ocp_target_id)

    source_networks, source_datastores = _vcenter_source_signals(db, mapping)
    mapped_networks = {
        m.get("source_network")
        for m in (mapping.network_mappings or [])
        if (m.get("target_network_name") or "").strip()
    }
    mapped_datastores = {
        m.get("source_datastore")
        for m in (mapping.storage_mappings or [])
        if (m.get("target_storage_class") or "").strip()
    }
    unmapped_networks = sorted(
        {n["name"] for n in source_networks} - mapped_networks
    )
    unmapped_datastores = sorted(
        {d["name"] for d in source_datastores} - mapped_datastores
    )

    target_status = "unknown"
    missing_storage_classes_on_target: list[str] = []
    missing_networks_on_target: list[str] = []
    warnings: list[str] = []

    if target is not None:
        target_status = target.status.value
        if target.status != OCPTargetStatus.active:
            warnings.append(
                f"OCP target '{target.name}' is {target.status.value} — "
                "discovery may be stale or auth has expired."
            )
        sc_names = {
            sc.get("name") for sc in (target.storage_classes or [])
        }
        net_names = {
            n.get("name") for n in (target.network_attachments or [])
        }
        for m in mapping.storage_mappings or []:
            ref = m.get("target_storage_class")
            if ref and ref not in sc_names:
                missing_storage_classes_on_target.append(ref)
        for m in mapping.network_mappings or []:
            ref = m.get("target_network_name")
            if ref and ref not in net_names:
                missing_networks_on_target.append(ref)
    else:
        warnings.append(
            "OCP target row not found — re-create the mapping or fix the target."
        )

    if not (mapping.namespace_mappings or []):
        warnings.append(
            "No namespace mapping configured — every VM will land in the cluster's default namespace."
        )

    ok = (
        not unmapped_networks
        and not unmapped_datastores
        and not missing_storage_classes_on_target
        and not missing_networks_on_target
    )
    return {
        "ok": ok,
        "target_status": target_status,
        "unmapped_networks": unmapped_networks,
        "unmapped_datastores": unmapped_datastores,
        "missing_storage_classes_on_target": sorted(set(missing_storage_classes_on_target)),
        "missing_networks_on_target": sorted(set(missing_networks_on_target)),
        "missing_namespaces_on_target": [],
        "warnings": warnings,
    }
