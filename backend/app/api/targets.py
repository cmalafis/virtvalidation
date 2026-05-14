"""OCP target cluster registry + ResourceMapping CRUD + plan-time mapping resolution.

Two logical groupings live in this file because they all share the
"plan generation needs real cluster resources, not placeholders" thread:

  - ``/api/sources/targets`` — register OCP target clusters (metadata
    only; the appliance does not authenticate to clusters). The
    per-cluster resource catalogs (TargetNetwork / TargetStorageClass /
    OCPTargetNamespace) are CRUD'd via separate routers.
  - ``/api/mappings`` — CRUD for ResourceMapping rows, with LLM-driven
    suggestion endpoints + a pre-flight check. Mappings are unique
    per ``(vcenter_source_id, ocp_target_id)`` pair.
"""

from __future__ import annotations

import logging
from collections import Counter

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
from app.models.plan import MigrationPlan
from app.models.target import (
    OCPTarget,
    ResourceMapping,
    ResourceMappingStatus,
)
from app.models.target_network import TargetNetwork
from app.models.target_storage_class import TargetStorageClass
from app.models.vcenter import VCenterSource
from app.models.vm import VM
from app.schemas.target import (
    MappingSuggestionResponse,
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
        raise HTTPException(status_code=404, detail=f"OCP target {target_id} not found")
    return target


@targets_router.get("", response_model=list[OCPTargetRead])
def list_targets(db: Session = Depends(get_db)) -> list[OCPTarget]:
    return list(db.scalars(select(OCPTarget).order_by(OCPTarget.name)).all())


@targets_router.post("", response_model=OCPTargetRead, status_code=status.HTTP_201_CREATED)
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
# Mappings router
# ---------------------------------------------------------------------------
mappings_router = APIRouter(tags=["resource-mappings"])


def _get_mapping_or_404(db: Session, mapping_id: int) -> ResourceMapping:
    mapping = db.get(ResourceMapping, mapping_id)
    if mapping is None:
        raise HTTPException(status_code=404, detail=f"Resource mapping {mapping_id} not found")
    return mapping


def _compute_mapping_status(mapping: ResourceMapping, db: Session) -> ResourceMappingStatus:
    """Walk the mapping payload + the cluster scope's source resources
    to determine completeness.

    ``complete``: every distinct source network + datastore in the
    vCenter scope has a non-null target.

    ``incomplete``: at least one source resource has no target
    selected.

    ``needs_review``: target row references a resource that's no
    longer present on the cluster (drift).

    Non-dict entries in ``network_mappings`` / ``storage_mappings``
    are skipped defensively — legacy payloads occasionally contain
    nulls or strings, and crashing the read because of a bad row is
    worse than ignoring it.
    """
    network_rows = [m for m in (mapping.network_mappings or []) if isinstance(m, dict)]
    storage_rows = [m for m in (mapping.storage_mappings or []) if isinstance(m, dict)]
    networks = {
        m.get("source_network")
        for m in network_rows
        if (m.get("target_network_name") or "").strip()
    }
    datastores = {
        m.get("source_datastore")
        for m in storage_rows
        if (m.get("target_storage_class") or "").strip()
    }

    vms = list(
        db.scalars(select(VM).where(VM.source_vcenter_id == mapping.vcenter_source_id)).all()
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
    if target is not None:
        target_sc_names = _target_sc_names(db, target)
        target_net_names = _target_net_names(db, target)
        # Only flag drift when we have an authoritative list. Empty
        # catalogs are legitimate (operator hasn't populated them yet);
        # we don't want to flip every mapping to needs_review just
        # because the catalogs are blank.
        if target_sc_names is not None:
            for m in storage_rows:
                referenced = m.get("target_storage_class")
                if referenced and referenced not in target_sc_names:
                    return ResourceMappingStatus.needs_review
        if target_net_names is not None:
            for m in network_rows:
                referenced = m.get("target_network_name")
                if referenced and referenced not in target_net_names:
                    return ResourceMappingStatus.needs_review

    return ResourceMappingStatus.complete


def _safe_compute_status(
    mapping: ResourceMapping, db: Session, context: str
) -> ResourceMappingStatus:
    """Defensive wrapper around ``_compute_mapping_status``.

    The recompute touches multiple tables (target_networks,
    target_storage_classes, vms) and walks JSON columns whose shape
    has changed across versions. A single bad row would otherwise 500
    the request that triggered the recompute. We'd rather surface a
    slightly stale status than block the operator from opening the
    editor or saving."""
    try:
        return _compute_mapping_status(mapping, db)
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception(
            "mapping.status.recompute_failed mapping_id=%s context=%s",
            mapping.id,
            context,
        )
        return mapping.status or ResourceMappingStatus.incomplete


def _target_net_names(db: Session, target: OCPTarget) -> set[str] | None:
    """Authoritative set of valid target network names for a cluster
    from the operator-declared :class:`TargetNetwork` catalog. Returns
    ``None`` when the catalog is empty so callers can skip drift
    checks rather than spuriously flagging needs_review."""
    declared = list(
        db.scalars(select(TargetNetwork).where(TargetNetwork.ocp_target_id == target.id)).all()
    )
    return {n.name for n in declared} if declared else None


def _target_sc_names(db: Session, target: OCPTarget) -> set[str] | None:
    declared = list(
        db.scalars(
            select(TargetStorageClass).where(TargetStorageClass.ocp_target_id == target.id)
        ).all()
    )
    return {s.name for s in declared} if declared else None


@mappings_router.get("", response_model=list[ResourceMappingRead])
def list_mappings(db: Session = Depends(get_db)) -> list[ResourceMapping]:
    return list(
        db.scalars(select(ResourceMapping).order_by(ResourceMapping.updated_at.desc())).all()
    )


@mappings_router.post("", response_model=ResourceMappingRead, status_code=status.HTTP_201_CREATED)
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

    # namespace_mappings is either a NamespaceStrategy (dict) or a list
    # of NamespaceMappingItem rows. Serialize each shape verbatim into
    # the JSON column.
    ns_payload: list | dict
    if isinstance(payload.namespace_mappings, list):
        ns_payload = [m.model_dump() for m in payload.namespace_mappings]
    else:
        ns_payload = payload.namespace_mappings.model_dump()

    mapping = ResourceMapping(
        name=payload.name,
        vcenter_source_id=payload.vcenter_source_id,
        ocp_target_id=payload.ocp_target_id,
        network_mappings=[m.model_dump() for m in payload.network_mappings],
        storage_mappings=[m.model_dump() for m in payload.storage_mappings],
        namespace_mappings=ns_payload,
    )
    db.add(mapping)
    try:
        db.flush()
    except IntegrityError as e:
        db.rollback()
        # Surface the existing mapping name so the frontend can deep-link
        # to it. Mappings are unique per (vcenter, target) pair under
        # ``uq_mapping_per_pair`` (see the multi-cluster target arch
        # migration); duplicates of any kind hit this branch.
        existing = db.scalar(
            select(ResourceMapping).where(
                ResourceMapping.vcenter_source_id == payload.vcenter_source_id,
                ResourceMapping.ocp_target_id == payload.ocp_target_id,
            )
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "detail": (
                        f"Mapping already exists for vCenter "
                        f"{payload.vcenter_source_id} → cluster "
                        f"{payload.ocp_target_id}: mapping id {existing.id}, "
                        f"name {existing.name!r}."
                    ),
                    "existing_mapping_id": existing.id,
                    "existing_mapping_name": existing.name,
                },
            ) from e
        raise HTTPException(
            status_code=409,
            detail=f"Mapping create violated a uniqueness constraint: {e}",
        ) from e
    mapping.status = _safe_compute_status(mapping, db, context="create_mapping")
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
    # Recompute status on read so drift surfaces in the editor without
    # an explicit refresh. We deliberately do NOT commit the recomputed
    # value: persisting on every GET caused 500s when the recompute
    # touched a partially-migrated DB, and a GET-side write also bumps
    # updated_at on every page load. The in-memory assignment is enough
    # for the response.
    mapping.status = _safe_compute_status(mapping, db, context="get_mapping")
    return mapping


@mappings_router.patch("/{mapping_id}", response_model=ResourceMappingRead)
def update_mapping(
    request: Request,
    mapping_id: int,
    payload: ResourceMappingUpdate,
    db: Session = Depends(get_db),
) -> ResourceMapping:
    mapping = _get_mapping_or_404(db, mapping_id)
    updates = payload.model_dump(exclude_unset=True)

    # Validate every referenced target_network_name / target_storage_class
    # exists in the operator-declared catalog (or in the legacy discovery
    # cache as a fallback) before persisting. Mapping editor reviews
    # depend on this — without it, the save can succeed with a stale
    # target name that plan generation then fails on at YAML-render time.
    target = db.get(OCPTarget, mapping.ocp_target_id)
    if target is not None:
        valid_nets = _target_net_names(db, target)
        valid_scs = _target_sc_names(db, target)
        if "network_mappings" in updates and updates["network_mappings"] is not None:
            for row in updates["network_mappings"]:
                ref = (row or {}).get("target_network_name")
                if ref and valid_nets is not None and ref not in valid_nets:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"target_network_name {ref!r} is not defined on "
                            f"cluster {target.name!r}. Add it under "
                            "OCP Targets → Networks first."
                        ),
                    )
        if "storage_mappings" in updates and updates["storage_mappings"] is not None:
            for row in updates["storage_mappings"]:
                ref = (row or {}).get("target_storage_class")
                if ref and valid_scs is not None and ref not in valid_scs:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"target_storage_class {ref!r} is not defined on "
                            f"cluster {target.name!r}. Add it under "
                            "OCP Targets → Storage Classes first."
                        ),
                    )

    if "network_mappings" in updates and updates["network_mappings"] is not None:
        mapping.network_mappings = list(updates["network_mappings"])
    if "storage_mappings" in updates and updates["storage_mappings"] is not None:
        mapping.storage_mappings = list(updates["storage_mappings"])
    if "namespace_mappings" in updates and updates["namespace_mappings"] is not None:
        # JSON column accepts either a list (legacy criteria rows) or a
        # dict (new NamespaceStrategy shape). Pass through verbatim;
        # the resolver dispatches on shape at plan-generation time.
        mapping.namespace_mappings = updates["namespace_mappings"]
    if "name" in updates and updates["name"] is not None:
        mapping.name = updates["name"]
    mapping.status = _safe_compute_status(mapping, db, context="update_mapping")
    db.commit()
    db.refresh(mapping)
    request.state.skip_audit_log = True
    return mapping


@mappings_router.delete("/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_mapping(
    request: Request,
    mapping_id: int,
    db: Session = Depends(get_db),
) -> None:
    """Delete a resource mapping.

    Returns 409 with the list of referencing plans when the mapping
    is still in use. Previously this relied on
    ``Plan.mapping_id ON DELETE SET NULL`` which silently orphaned
    plans — operators couldn't tell why their plan's MTV YAML export
    suddenly emitted placeholder names. The 409 body matches the same
    shape used by ``target_entities.delete_network`` so the frontend
    can render referencing rows consistently."""
    mapping = _get_mapping_or_404(db, mapping_id)
    # Block deletion when any plan's ``mapping_ids`` snapshot references
    # this mapping — the YAML emitter looks them up by id at export
    # time. JSON-contains semantics vary across dialects so we filter
    # in Python; plan count is bounded by operator usage and this
    # endpoint isn't on a hot path.
    candidate_plans = list(
        db.scalars(select(MigrationPlan).where(MigrationPlan.mapping_ids.is_not(None))).all()
    )
    referencing = [p for p in candidate_plans if mapping.id in (p.mapping_ids or [])]
    if referencing:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": (
                    f"Mapping {mapping.name!r} is still referenced by "
                    f"{len(referencing)} plan(s); delete or reassign them first."
                ),
                "referenced_by": [
                    {
                        "plan_id": p.id,
                        "plan_name": p.name,
                        "status": p.status,
                    }
                    for p in referencing
                ],
            },
        )
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
def _vcenter_source_signals(db: Session, mapping: ResourceMapping) -> tuple[list[dict], list[dict]]:
    """Aggregate source networks/datastores in the mapping's vCenter
    scope. Returns lists shaped for the suggestion prompts."""
    vms = list(
        db.scalars(select(VM).where(VM.source_vcenter_id == mapping.vcenter_source_id)).all()
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


@mappings_router.post("/{mapping_id}/suggest-network", response_model=MappingSuggestionResponse)
def suggest_networks(mapping_id: int, db: Session = Depends(get_db)) -> dict:
    """Ask the LLM to match source vSphere networks onto the operator's
    declared :class:`TargetNetwork` rows for the mapping's target
    cluster. The operator catalog is the only source of truth — the
    appliance does not authenticate to clusters for live discovery."""
    mapping = _get_mapping_or_404(db, mapping_id)
    target = db.get(OCPTarget, mapping.ocp_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="OCP target not found")

    declared = list(
        db.scalars(select(TargetNetwork).where(TargetNetwork.ocp_target_id == target.id)).all()
    )
    if not declared:
        raise HTTPException(
            status_code=400,
            detail="Define target networks for this cluster first.",
        )

    networks, _ = _vcenter_source_signals(db, mapping)
    target_payload = [
        {
            "name": n.name,
            "type": n.network_type.value,
            "namespace": n.namespace,
        }
        for n in declared
    ]
    try:
        result = suggest_network_mappings(sources=networks, targets=target_payload)
    except SuggestionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return result


@mappings_router.post("/{mapping_id}/suggest-storage", response_model=MappingSuggestionResponse)
def suggest_storage(mapping_id: int, db: Session = Depends(get_db)) -> dict:
    mapping = _get_mapping_or_404(db, mapping_id)
    target = db.get(OCPTarget, mapping.ocp_target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="OCP target not found")

    declared = list(
        db.scalars(
            select(TargetStorageClass).where(TargetStorageClass.ocp_target_id == target.id)
        ).all()
    )
    if not declared:
        raise HTTPException(
            status_code=400,
            detail="Define target storage classes for this cluster first.",
        )

    _, datastores = _vcenter_source_signals(db, mapping)
    target_payload = [
        {
            "name": s.name,
            "provisioner": "operator-declared",
            "is_default": s.is_default,
            "access_modes": [s.access_mode.value],
        }
        for s in declared
    ]
    try:
        result = suggest_storage_mappings(sources=datastores, targets=target_payload)
    except SuggestionError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return result


# ---------------------------------------------------------------------------
# Pre-flight check
# ---------------------------------------------------------------------------
@mappings_router.post("/{mapping_id}/preflight", response_model=PreflightCheckResponse)
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
    unmapped_networks = sorted({n["name"] for n in source_networks} - mapped_networks)
    unmapped_datastores = sorted({d["name"] for d in source_datastores} - mapped_datastores)

    target_status = "unknown"
    missing_storage_classes_on_target: list[str] = []
    missing_networks_on_target: list[str] = []
    warnings: list[str] = []

    if target is not None:
        target_status = target.status.value
        # The "discovery may be stale" warning was removed: target
        # entities (TargetNetwork / TargetStorageClass) are now
        # operator-declared and don't depend on the auth-based
        # discovery lifecycle. A non-active status no longer implies
        # a broken mapping.
        sc_names = _target_sc_names(db, target)
        net_names = _target_net_names(db, target)
        if sc_names is not None:
            for m in mapping.storage_mappings or []:
                ref = m.get("target_storage_class")
                if ref and ref not in sc_names:
                    missing_storage_classes_on_target.append(ref)
        if net_names is not None:
            for m in mapping.network_mappings or []:
                ref = m.get("target_network_name")
                if ref and ref not in net_names:
                    missing_networks_on_target.append(ref)
    else:
        warnings.append("OCP target row not found — re-create the mapping or fix the target.")

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
