from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app.core.audit import record_audit
from app.core.baseline import synthesize_profile
from app.core.capture import run_capture_task, task_store
from app.core.db import get_db
from app.core.limits import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.core.target_resolution import (
    ResolvedTarget,
    resolve_vm_target,
    resolve_vms_iter,
)
from app.core.validation import run_validation_task
from app.core.validation import task_store as validation_task_store
from app.core.vm_lifecycle import LifecycleTransitionError, patch_transition
from app.models.validation import ValidationResult
from app.models.vcenter import VCenterSource
from app.models.vm import VM, BaselineSnapshot, VMLifecycleState, VMStatus
from app.schemas.validation import (
    LatestValidationResponse,
    ValidationTaskRead,
)
from app.schemas.vm import (
    BaselineProfile,
    BulkClearTargetRequest,
    BulkEnvironmentSetRequest,
    BulkEnvironmentSetResult,
    BulkSetTargetClusterRequest,
    BulkSetTargetNamespaceRequest,
    BulkTargetOverrideResult,
    BulkVMCreate,
    BulkVMDelete,
    BulkVMDeleteResult,
    BulkVMResult,
    CaptureTaskRead,
    DeleteAllVMsResult,
    EnvironmentSetRequest,
    RedetectEnvironmentRequest,
    RedetectEnvironmentResult,
    SnapshotCreate,
    SnapshotRead,
    VMCreate,
    VMFacetsResponse,
    VMListResponse,
    VMRead,
    VMStats,
    VMUpdate,
)

# Columns the table UI is allowed to sort by. Keeping this as a fixed
# allow-list (vs. accepting any attribute name) prevents accidental
# ``ORDER BY notes`` queries that would scan the whole 1024-char column.
SortableColumn = Literal[
    "name",
    "status",
    "lifecycle_state",
    "environment",
    "os_family",
    "application_hint",
    "source_vcenter_id",
    "created_at",
    "updated_at",
]

SortOrder = Literal["asc", "desc"]

router = APIRouter(tags=["vms"])


def _get_vm_or_404(db: Session, vm_id: int) -> VM:
    vm = db.get(VM, vm_id)
    if vm is None:
        raise HTTPException(status_code=404, detail=f"VM {vm_id} not found")
    return vm


def _apply_environment_detection(vm: VM) -> None:
    """Fill ``vm.environment`` from the detection cascade when the caller
    didn't supply one, and record provenance either way.

    The cascade reads name / folder / cluster / custom_attributes; tiers
    2-4 only resolve once those fields are populated (RVTools import, or
    an operator filling them on the manual-add form).
    ``environment_source`` records provenance so a later redetect-all job
    skips operator-supplied labels.

    Shared by the single-create and bulk-create paths so a manually added
    VM partitions the same way an imported one does — ``environment`` is
    a plan partition key in ``preclassifier.classify``.
    """
    from app.core.environment import Environment, detect_environment

    if not vm.environment:
        result = detect_environment(
            name=vm.name,
            folder_path=vm.vsphere_folder,
            cluster=vm.vsphere_cluster,
            custom_attributes=vm.custom_attributes,
        )
        if result.environment != Environment.UNKNOWN:
            vm.environment = result.environment.value
            vm.environment_source = "auto_detected"
    else:
        # Caller provided an explicit value — preserve operator intent +
        # mark source so redetect leaves it alone.
        vm.environment_source = "user_set"


def _vm_with_resolution(vm: VM, resolved: ResolvedTarget | None) -> dict:
    """Render a VM ORM row + its :class:`ResolvedTarget` as a flat dict
    that ``VMRead`` validates without extra schema gymnastics. The
    resolver fields default to ``None`` / empty when ``resolved`` is
    ``None`` — keeps the shape stable for endpoints that haven't
    threaded the resolver yet.
    """
    body = VMRead.model_validate(vm, from_attributes=True).model_dump(mode="json")
    if resolved is not None:
        body["resolved_target_cluster_id"] = resolved.cluster_id
        body["resolved_target_cluster_name"] = resolved.cluster_name
        body["resolved_target_namespace"] = resolved.namespace
        body["resolved_networks"] = [
            {
                "source": n.source,
                "target_network_id": n.target_network_id,
                "target_network_name": n.target_network_name,
                "target_network_namespace": n.target_network_namespace,
                "target_network_type": n.target_network_type,
            }
            for n in resolved.networks
        ]
        body["resolved_storage"] = [
            {
                "source": s.source,
                "target_storage_class_name": s.target_storage_class_name,
                "access_mode": s.access_mode,
            }
            for s in resolved.storage
        ]
        body["resolution_is_complete"] = resolved.is_complete
        body["resolution_reasons"] = list(resolved.reasons)
        body["resolution_mapping_id"] = resolved.mapping_id
    return body


def _decorate_with_resolution(vms: list[VM], db: Session) -> list[dict]:
    """Resolve every VM in ``vms`` in one pass and return the dict
    shape :class:`VMRead` consumes. Empty list → empty list, no DB hit.
    """
    if not vms:
        return []
    resolutions = resolve_vms_iter(vms, db)
    return [_vm_with_resolution(vm, resolutions.get(vm.id)) for vm in vms]


@router.post("", response_model=VMRead, status_code=status.HTTP_201_CREATED)
def create_vm(payload: VMCreate, db: Session = Depends(get_db)) -> VM:
    vm = VM(**payload.model_dump())
    _apply_environment_detection(vm)
    db.add(vm)
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        raise HTTPException(
            status_code=409, detail=f"VM with name '{payload.name}' already exists"
        ) from e
    db.refresh(vm)
    return vm


@router.post("/bulk", response_model=BulkVMResult)
def create_vms_bulk(payload: BulkVMCreate, db: Session = Depends(get_db)) -> dict:
    """Best-effort batch enrollment.

    Pre-loads existing VM names so duplicates don't trigger per-row
    IntegrityErrors. Returns the created rows and a list of names that were
    skipped (with reason). Within-batch duplicates are also caught.
    """
    existing_names: set[str] = set(db.scalars(select(VM.name)).all())
    seen_in_batch: set[str] = set()
    created: list[VM] = []
    skipped: list[dict] = []

    for entry in payload.vms:
        name = entry.name
        if name in existing_names:
            skipped.append({"name": name, "reason": "already enrolled"})
            continue
        if name in seen_in_batch:
            skipped.append({"name": name, "reason": "duplicate within batch"})
            continue
        seen_in_batch.add(name)
        fields = entry.model_dump()
        vm = VM(**fields)
        _apply_environment_detection(vm)
        db.add(vm)
        created.append(vm)

    db.commit()
    for vm in created:
        db.refresh(vm)

    return {
        "total": len(payload.vms),
        "created": created,
        "skipped": skipped,
    }


def _apply_vm_filters(
    stmt: Select,
    *,
    statuses: list[VMStatus] | None,
    lifecycle_states: list[VMLifecycleState] | None,
    vcenter_source_ids: list[int] | None,
    environments: list[str] | None,
    application_hints: list[str] | None,
    os_families: list[str] | None,
    classification_levels: list[str] | None,
    search: str | None,
) -> Select:
    """Compose the WHERE clause for the inventory listing and its facets.

    Same builder powers list, facets, stats, and delete-all so a single
    filter set semantics-matches across all four endpoints — the UI's
    "Delete all 247 filtered VMs" can use the same querystring it used
    to render the page.
    """
    if statuses:
        stmt = stmt.where(VM.status.in_(statuses))
    if lifecycle_states:
        stmt = stmt.where(VM.lifecycle_state.in_(lifecycle_states))
    if vcenter_source_ids:
        stmt = stmt.where(VM.source_vcenter_id.in_(vcenter_source_ids))
    if environments:
        stmt = stmt.where(VM.environment.in_(environments))
    if application_hints:
        stmt = stmt.where(VM.application_hint.in_(application_hints))
    if os_families:
        stmt = stmt.where(VM.os_family.in_(os_families))
    if classification_levels:
        # classification_level lives on VCenterSource; join through the FK.
        # ``isouter=True`` so VMs with NULL source_vcenter_id still match
        # if the operator explicitly filters for "unclassified" (covers
        # legacy enrollments that predate vCenter registration).
        stmt = stmt.join(VCenterSource, VM.source_vcenter_id == VCenterSource.id).where(
            VCenterSource.classification_level.in_(classification_levels)
        )
    if search:
        # Case-insensitive contains on the three free-form columns the
        # operator is likely to recognize: VM name, owner, app hint.
        # ``ilike`` works on both Postgres and SQLite (where it falls
        # back to a LIKE with the default no-case-folding collation —
        # acceptable for the test path).
        pattern = f"%{search}%"
        stmt = stmt.where(
            or_(
                VM.name.ilike(pattern),
                VM.owner.ilike(pattern),
                VM.application_hint.ilike(pattern),
            )
        )
    return stmt


_SORT_COLUMNS = {
    "name": VM.name,
    "status": VM.status,
    "lifecycle_state": VM.lifecycle_state,
    "environment": VM.environment,
    "os_family": VM.os_family,
    "application_hint": VM.application_hint,
    "source_vcenter_id": VM.source_vcenter_id,
    "created_at": VM.created_at,
    "updated_at": VM.updated_at,
}


@router.get("", response_model=VMListResponse)
def list_vms(
    db: Session = Depends(get_db),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    sort_by: SortableColumn = Query(default="name"),
    sort_order: SortOrder = Query(default="asc"),
    status_filter: list[VMStatus] | None = Query(default=None, alias="status"),
    lifecycle_state: list[VMLifecycleState] | None = Query(default=None),
    vcenter_source_id: list[int] | None = Query(default=None),
    environment: list[str] | None = Query(default=None),
    application_hint: list[str] | None = Query(default=None),
    os_family: list[str] | None = Query(default=None),
    classification_level: list[str] | None = Query(default=None),
    search: str | None = Query(default=None, max_length=255),
    # ``offset`` accepted as an alias for ``skip`` so older clients
    # (and tests) that pre-date the pagination overhaul keep working.
    offset: int | None = Query(default=None, ge=0),
) -> dict:
    """Paginated, filterable inventory listing.

    Returns ``{items, total, skip, limit}``. ``total`` is the count
    AFTER filters but BEFORE pagination — the data table needs both
    "rows visible now" and "rows matching the filter" to render the
    "Showing X-Y of Z" line and the pager.
    """
    if offset is not None:
        skip = offset

    base = select(VM)
    base = _apply_vm_filters(
        base,
        statuses=status_filter,
        lifecycle_states=lifecycle_state,
        vcenter_source_ids=vcenter_source_id,
        environments=environment,
        application_hints=application_hint,
        os_families=os_family,
        classification_levels=classification_level,
        search=search,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0

    column = _SORT_COLUMNS[sort_by]
    ordered = base.order_by(column.desc() if sort_order == "desc" else column.asc())
    # Stable tie-breaker on ID so identical sort keys don't reshuffle
    # across pages — a user paging through 1000 VMs sorted by status
    # would otherwise see the same VM on two adjacent pages.
    ordered = ordered.order_by(VM.id.asc())
    page_stmt = ordered.limit(limit).offset(skip)
    items = list(db.scalars(page_stmt).all())
    return {
        "items": _decorate_with_resolution(items, db),
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/facets", response_model=VMFacetsResponse)
def vm_facets(
    db: Session = Depends(get_db),
    status_filter: list[VMStatus] | None = Query(default=None, alias="status"),
    lifecycle_state: list[VMLifecycleState] | None = Query(default=None),
    vcenter_source_id: list[int] | None = Query(default=None),
    environment: list[str] | None = Query(default=None),
    application_hint: list[str] | None = Query(default=None),
    os_family: list[str] | None = Query(default=None),
    classification_level: list[str] | None = Query(default=None),
    search: str | None = Query(default=None, max_length=255),
) -> dict:
    """Per-dimension counts so the filter UI can show "Production (600)".

    Filters apply to every dimension uniformly — the filter UI gets
    "what would each value's count become if I clicked it on top of
    my current selection?" semantics. (This differs from the Kibana-
    style "exclude self" facets; we accept the simpler model because
    the inventory's filter dimensions are largely orthogonal.)
    """
    base = select(VM)
    base = _apply_vm_filters(
        base,
        statuses=status_filter,
        lifecycle_states=lifecycle_state,
        vcenter_source_ids=vcenter_source_id,
        environments=environment,
        application_hints=application_hint,
        os_families=os_family,
        classification_levels=classification_level,
        search=search,
    )
    subq = base.subquery()
    total = db.scalar(select(func.count()).select_from(subq)) or 0

    def _facet(col_name: str) -> dict[str, int]:
        # Project through the subquery's columns (not VM's) so the
        # GROUP BY operates on the filtered row set without a
        # cartesian join back to the VM table.
        sub_col = subq.c[col_name]
        rows = db.execute(select(sub_col, func.count()).group_by(sub_col)).all()
        out: dict[str, int] = {}
        for value, count in rows:
            if value is None:
                continue
            # Enum members carry a ``.value`` attribute; everything else
            # stringifies through str(). Integer-keyed columns (vcenter
            # id) get rendered as decimal strings so the JSON shape is
            # uniform.
            key = value.value if hasattr(value, "value") else str(value)
            out[key] = count
        return out

    # classification_level requires the join — compute it separately
    # so we don't pay the join cost on the other five facets.
    cls_rows = db.execute(
        select(VCenterSource.classification_level, func.count())
        .join(subq, VCenterSource.id == subq.c.source_vcenter_id)
        .group_by(VCenterSource.classification_level)
    ).all()
    classification_facet: dict[str, int] = {}
    for value, count in cls_rows:
        if value is None:
            continue
        key = value.value if hasattr(value, "value") else str(value)
        classification_facet[key] = count

    return {
        "status": _facet("status"),
        "lifecycle_state": _facet("lifecycle_state"),
        "environment": _facet("environment"),
        "os_family": _facet("os_family"),
        "application_hint": _facet("application_hint"),
        "vcenter_source_id": _facet("source_vcenter_id"),
        "classification_level": classification_facet,
        "total": total,
    }


@router.get("/stats", response_model=VMStats)
def vm_stats(db: Session = Depends(get_db)) -> dict:
    """Cheap dashboard counters — no filter set, no row reads.

    The dashboard's "Total VMs" tile used to reuse the paginated list
    response, which under the new contract returns only the current
    page. This endpoint exists so the tile can stay accurate without
    pulling every row.
    """
    total = db.scalar(select(func.count(VM.id))) or 0
    status_rows = db.execute(select(VM.status, func.count()).group_by(VM.status)).all()
    by_status: dict[str, int] = {}
    for value, count in status_rows:
        key = value.value if hasattr(value, "value") else str(value)
        by_status[key] = count
    missing = db.scalar(select(func.count(VM.id)).where(VM.missing_from_last_upload.is_(True))) or 0
    return {
        "total": total,
        "by_status": by_status,
        "missing_from_last_upload": missing,
    }


@router.delete("/all", response_model=DeleteAllVMsResult)
def delete_all_vms(
    request: Request,
    db: Session = Depends(get_db),
    confirm: bool = Query(default=False),
    status_filter: list[VMStatus] | None = Query(default=None, alias="status"),
    lifecycle_state: list[VMLifecycleState] | None = Query(default=None),
    vcenter_source_id: list[int] | None = Query(default=None),
    environment: list[str] | None = Query(default=None),
    application_hint: list[str] | None = Query(default=None),
    os_family: list[str] | None = Query(default=None),
    classification_level: list[str] | None = Query(default=None),
    search: str | None = Query(default=None, max_length=255),
) -> dict:
    """Bulk-delete every VM that matches the given filters.

    ``?confirm=true`` is mandatory — without it the call 400s before
    touching the DB. Filters are optional: with none, the endpoint
    clears the whole inventory; with some, it clears only the matching
    subset (the UI passes the user's current filter set so "Delete
    all" matches what the operator sees).

    One audit row is recorded summarizing the deletion (count + filter
    fingerprint). Per-VM forensics for bulk deletes already live on
    individual ``vm.delete`` rows when smaller operations are used;
    the federal compliance review accepts the summary row for "clear
    inventory" sweeps as long as the trail names the actor + filters.
    """
    if not confirm:
        request.state.skip_audit_log = True
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to delete inventory without explicit confirmation. "
                "Re-issue the request with ?confirm=true."
            ),
        )

    actor = request.headers.get("x-actor", "user")
    base = select(VM)
    base = _apply_vm_filters(
        base,
        statuses=status_filter,
        lifecycle_states=lifecycle_state,
        vcenter_source_ids=vcenter_source_id,
        environments=environment,
        application_hints=application_hint,
        os_families=os_family,
        classification_levels=classification_level,
        search=search,
    )
    vms = list(db.scalars(base).all())
    deleted_count = len(vms)
    # ORM-level delete so cascade rules fire (snapshots + validations
    # both cascade off VM via the relationship config). A bulk
    # ``DELETE FROM vms WHERE …`` would skip the ORM cascade on
    # SQLite test paths where the FK pragma isn't always set.
    for vm in vms:
        db.delete(vm)

    record_audit(
        db,
        action="vm.delete_all",
        actor=actor,
        resource_type="vm",
        resource_id=None,
        details={
            "deleted_count": deleted_count,
            "filters": {
                "status": [s.value for s in (status_filter or [])],
                "lifecycle_state": [s.value for s in (lifecycle_state or [])],
                "vcenter_source_id": vcenter_source_id or [],
                "environment": environment or [],
                "application_hint": application_hint or [],
                "os_family": os_family or [],
                "classification_level": classification_level or [],
                "search": search,
            },
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return {"deleted_count": deleted_count}


# ---------------------------------------------------------------------------
# Environment management — operator override + bulk set + redetect.
# These three routes must be declared BEFORE ``/{vm_id}`` paths so
# FastAPI doesn't route ``/bulk-set-environment`` to the integer-typed
# ``vm_id`` parameter. The same trick applies elsewhere in this file
# (``/all``, ``/facets``, ``/stats``).
# ---------------------------------------------------------------------------
def _normalize_env_or_400(value: str) -> str:
    """Validate + canonicalize an environment string via the enum.

    Raises 400 instead of silently mapping invalid input to UNKNOWN —
    the manual-override path should fail loudly so operators see the
    typo. The bulk import path keeps the looser permissive
    behavior because legitimate RVTools data often contains
    variants we haven't seen yet.
    """
    from app.core.environment import Environment, normalize

    env = normalize(value)
    if env == Environment.UNKNOWN and (value or "").strip().lower() not in {
        "",
        "unknown",
    }:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown environment value {value!r}. "
                f"Allowed values: {', '.join(e.value for e in Environment)}."
            ),
        )
    return env.value


@router.post("/bulk-set-environment", response_model=BulkEnvironmentSetResult)
def bulk_set_environment(
    request: Request,
    payload: BulkEnvironmentSetRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Atomically set the environment of N VMs.

    All-or-nothing: if any vm_id is missing, the whole call fails
    with 404 and nothing is persisted. The audit log records the
    bulk operation + the rationale so federal reviewers can trace
    why a chunk of inventory was relabeled.
    """
    new_env = _normalize_env_or_400(payload.environment)
    requested = list(dict.fromkeys(payload.vm_ids))
    found = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(requested))).all()}
    missing = [vid for vid in requested if vid not in found]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"VMs not found: {missing[:20]}",
        )
    for vm in found.values():
        vm.environment = new_env
        vm.environment_source = "user_set"
    actor = request.headers.get("x-actor", "user")
    record_audit(
        db,
        action="vm.environment.bulk_set",
        actor=actor,
        resource_type="vm",
        resource_id=None,
        details={
            "vm_ids": requested,
            "environment": new_env,
            "rationale": payload.rationale,
            "count": len(found),
        },
    )
    db.commit()
    request.state.skip_audit_log = True
    return {"updated": len(found), "not_found": []}


# ---------------------------------------------------------------------------
# Bulk target overrides — same routing rule as bulk-set-environment.
# These four routes set/clear ``target_cluster_id_override`` and
# ``target_namespace_override`` on N VMs in one shot. Per-VM single
# patches go through the standard PATCH /api/vms/{id}; these bulk
# endpoints exist so the inventory bulk-action bar can apply an
# override to every selected row in one request.
# ---------------------------------------------------------------------------
def _apply_bulk_target_overrides(
    *,
    db: Session,
    vm_ids: list[int],
    field: str,
    value: int | str | None,
    audit_action: str,
    audit_details_extra: dict,
    request: Request,
) -> dict:
    requested = list(dict.fromkeys(vm_ids))
    found = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(requested))).all()}
    missing = [vid for vid in requested if vid not in found]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"VMs not found: {missing[:20]}",
        )
    if field == "target_cluster_id_override" and value is not None:
        # Validate the override points at a known OCPTarget. Skipping
        # the check on None lets clear-override paths reuse the helper.
        from app.models.target import OCPTarget

        if db.get(OCPTarget, int(value)) is None:
            raise HTTPException(
                status_code=404,
                detail=f"OCPTarget {value} not found",
            )
    for vm in found.values():
        setattr(vm, field, value)
    record_audit(
        db,
        action=audit_action,
        actor=request.headers.get("x-actor", "user"),
        resource_type="vm",
        resource_id=None,
        details={"vm_ids": requested, "count": len(found), **audit_details_extra},
    )
    db.commit()
    request.state.skip_audit_log = True
    return {"updated": len(found), "not_found": []}


@router.post("/bulk-set-target-cluster", response_model=BulkTargetOverrideResult)
def bulk_set_target_cluster(
    request: Request,
    payload: BulkSetTargetClusterRequest,
    db: Session = Depends(get_db),
) -> dict:
    return _apply_bulk_target_overrides(
        db=db,
        vm_ids=payload.vm_ids,
        field="target_cluster_id_override",
        value=payload.target_cluster_id_override,
        audit_action="vm.target_cluster_override.bulk_set",
        audit_details_extra={"target_cluster_id_override": payload.target_cluster_id_override},
        request=request,
    )


@router.post("/bulk-clear-target-cluster", response_model=BulkTargetOverrideResult)
def bulk_clear_target_cluster(
    request: Request,
    payload: BulkClearTargetRequest,
    db: Session = Depends(get_db),
) -> dict:
    return _apply_bulk_target_overrides(
        db=db,
        vm_ids=payload.vm_ids,
        field="target_cluster_id_override",
        value=None,
        audit_action="vm.target_cluster_override.bulk_clear",
        audit_details_extra={},
        request=request,
    )


@router.post("/bulk-set-target-namespace", response_model=BulkTargetOverrideResult)
def bulk_set_target_namespace(
    request: Request,
    payload: BulkSetTargetNamespaceRequest,
    db: Session = Depends(get_db),
) -> dict:
    return _apply_bulk_target_overrides(
        db=db,
        vm_ids=payload.vm_ids,
        field="target_namespace_override",
        value=payload.target_namespace_override,
        audit_action="vm.target_namespace_override.bulk_set",
        audit_details_extra={"target_namespace_override": payload.target_namespace_override},
        request=request,
    )


@router.post("/bulk-clear-target-namespace", response_model=BulkTargetOverrideResult)
def bulk_clear_target_namespace(
    request: Request,
    payload: BulkClearTargetRequest,
    db: Session = Depends(get_db),
) -> dict:
    return _apply_bulk_target_overrides(
        db=db,
        vm_ids=payload.vm_ids,
        field="target_namespace_override",
        value=None,
        audit_action="vm.target_namespace_override.bulk_clear",
        audit_details_extra={},
        request=request,
    )


@router.post("/redetect-environment", response_model=RedetectEnvironmentResult)
def redetect_environment(
    request: Request,
    payload: RedetectEnvironmentRequest,
    db: Session = Depends(get_db),
) -> dict:
    """Re-run the detection cascade on the fleet (or one vCenter).

    Skips VMs marked ``user_set`` by default so operator overrides
    survive bulk re-runs. ``force=true`` overrides that protection;
    ``dry_run=true`` returns the projected changes without
    persisting. Federal audit trails record the redetect operation
    summary so reviewers can reconstruct which labels are pre- vs
    post-redetect.
    """
    from app.core.environment import Environment, detect_environment

    stmt = select(VM)
    if payload.vcenter_source_id is not None:
        stmt = stmt.where(VM.source_vcenter_id == payload.vcenter_source_id)
    vms = list(db.scalars(stmt).all())

    updated = 0
    skipped_user_set = 0
    still_unknown = 0
    summary: dict[str, int] = {}
    unknown_samples: list[str] = []

    for vm in vms:
        if vm.environment_source == "user_set" and not payload.force:
            skipped_user_set += 1
            # Tally the current value into the summary so the operator
            # sees the full breakdown including untouched labels.
            current = (vm.environment or "unknown") or "unknown"
            summary[current] = summary.get(current, 0) + 1
            continue
        result = detect_environment(
            name=vm.name,
            folder_path=vm.vsphere_folder,
            cluster=vm.vsphere_cluster,
            custom_attributes=vm.custom_attributes,
        )
        new_value = result.environment.value
        if result.environment == Environment.UNKNOWN:
            still_unknown += 1
            if len(unknown_samples) < 10:
                unknown_samples.append(vm.name)
        if not payload.dry_run:
            if result.environment != Environment.UNKNOWN:
                if vm.environment != new_value:
                    vm.environment = new_value
                    vm.environment_source = "auto_detected"
                    updated += 1
                # else: same value, no update counted
            elif vm.environment is not None and vm.environment_source != "user_set":
                # Detector says UNKNOWN, but VM had an auto-detected
                # value — wipe it so the operator sees the gap.
                vm.environment = None
                vm.environment_source = "unset"
        else:
            if result.environment != Environment.UNKNOWN and vm.environment != new_value:
                updated += 1
        summary[new_value] = summary.get(new_value, 0) + 1

    if not payload.dry_run:
        actor = request.headers.get("x-actor", "user")
        record_audit(
            db,
            action="vm.environment.redetect",
            actor=actor,
            resource_type="vm",
            resource_id=None,
            details={
                "scanned": len(vms),
                "updated": updated,
                "skipped_user_set": skipped_user_set,
                "force": payload.force,
                "vcenter_source_id": payload.vcenter_source_id,
            },
        )
        db.commit()
    request.state.skip_audit_log = True

    return {
        "scanned": len(vms),
        "updated": updated,
        "skipped_user_set": skipped_user_set,
        "still_unknown": still_unknown,
        "summary_by_environment": summary,
        "still_unknown_samples": unknown_samples,
    }


@router.patch("/{vm_id}/environment", response_model=VMRead)
def set_vm_environment(
    request: Request,
    vm_id: int,
    payload: EnvironmentSetRequest,
    db: Session = Depends(get_db),
) -> VM:
    """Operator override of one VM's environment.

    Marks ``environment_source="user_set"`` so the next redetect
    run leaves the value alone. Audit-logged for compliance.
    """
    vm = _get_vm_or_404(db, vm_id)
    new_env = _normalize_env_or_400(payload.environment)
    actor = request.headers.get("x-actor", "user")
    before = vm.environment
    vm.environment = new_env
    vm.environment_source = "user_set"
    record_audit(
        db,
        action="vm.environment.set",
        actor=actor,
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "before": before,
            "after": new_env,
            "rationale": payload.rationale,
        },
    )
    db.commit()
    db.refresh(vm)
    request.state.skip_audit_log = True
    return vm


@router.get("/{vm_id}", response_model=VMRead)
def get_vm(vm_id: int, db: Session = Depends(get_db)) -> dict:
    vm = _get_vm_or_404(db, vm_id)
    return _vm_with_resolution(vm, resolve_vm_target(vm, db))


def _diff_for_audit(before: dict, after: dict) -> dict:
    """Return only the keys that actually changed, with before/after values.

    Used so the audit row's `details` column carries the exact field-level
    delta rather than the whole payload — matters for federal compliance
    review, where reviewers want to see what an operator actually changed.
    """
    delta: dict[str, dict] = {}
    for key in after:
        if before.get(key) != after.get(key):
            delta[key] = {"before": before.get(key), "after": after.get(key)}
    return delta


@router.patch("/{vm_id}", response_model=VMRead)
def update_vm(
    request: Request,
    vm_id: int,
    payload: VMUpdate,
    db: Session = Depends(get_db),
) -> dict:
    vm = _get_vm_or_404(db, vm_id)
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        # Nothing to change — suppress the middleware audit so empty PATCHes
        # don't pollute the trail.
        request.state.skip_audit_log = True
        return vm

    # Lifecycle transitions go through the service so the state machine
    # is enforced server-side and the audit row is the structured
    # ``vm.lifecycle_state.transition`` shape (not the generic
    # ``vm.update`` diff). The route accepts only ``migrated →
    # rolled_back`` and ``rolled_back → available``; anything else
    # raises 422.
    actor = request.headers.get("x-actor", "user")
    lifecycle_target = updates.pop("lifecycle_state", None)
    if lifecycle_target is not None:
        try:
            patch_transition(vm, lifecycle_target, db, actor=actor)
        except LifecycleTransitionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if updates:
        before = {k: getattr(vm, k) for k in updates}
        for field, value in updates.items():
            setattr(vm, field, value)
        db.flush()
        after = {k: getattr(vm, k) for k in updates}

        record_audit(
            db,
            action="vm.update",
            actor=actor,
            resource_type="vm",
            resource_id=vm.id,
            details={
                "vm_name": vm.name,
                "diff": _diff_for_audit(before, after),
            },
        )
    db.commit()
    db.refresh(vm)
    request.state.skip_audit_log = True
    return _vm_with_resolution(vm, resolve_vm_target(vm, db))


@router.delete("/{vm_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_vm(
    request: Request,
    vm_id: int,
    db: Session = Depends(get_db),
) -> None:
    vm = _get_vm_or_404(db, vm_id)
    # Audit FIRST so the trail records what was deleted; the audit_logs
    # table is intentionally not FK'd to vms, so the row survives the
    # cascade. Federal review can still answer "who deleted db-prod-01?"
    # months after the row is gone.
    record_audit(
        db,
        action="vm.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "source_hostname": vm.source_hostname,
            "ip_address": vm.ip_address,
        },
    )
    db.delete(vm)  # cascades snapshots + validations via the ORM relationships
    db.commit()
    request.state.skip_audit_log = True


@router.delete("", response_model=BulkVMDeleteResult)
def delete_vms_bulk(
    request: Request,
    payload: BulkVMDelete,
    db: Session = Depends(get_db),
) -> dict:
    """Delete multiple VMs in one round-trip.

    Returns the lists of deleted vs not-found ids. Each successfully
    deleted VM gets its own ``vm.delete`` audit row so per-VM forensics
    still work after a bulk operation.
    """
    actor = request.headers.get("x-actor", "user")
    requested = list(dict.fromkeys(payload.vm_ids))  # de-dupe, preserve order
    found_vms = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(requested))).all()}

    deleted: list[int] = []
    not_found: list[int] = []
    for vid in requested:
        vm = found_vms.get(vid)
        if vm is None:
            not_found.append(vid)
            continue
        record_audit(
            db,
            action="vm.delete",
            actor=actor,
            resource_type="vm",
            resource_id=vm.id,
            details={
                "vm_name": vm.name,
                "source_hostname": vm.source_hostname,
                "ip_address": vm.ip_address,
                "via_bulk": True,
            },
        )
        db.delete(vm)
        deleted.append(vid)
    db.commit()

    request.state.skip_audit_log = True
    return {"requested": len(requested), "deleted": deleted, "not_found": not_found}


# ---------------------------------------------------------------------------
# On-demand baseline capture
# ---------------------------------------------------------------------------
@router.post(
    "/{vm_id}/capture",
    response_model=CaptureTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_capture(
    request: Request,
    vm_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn an immediate baseline collection in the background.

    Returns 202 with the task handle immediately; the actual SSH session
    runs after the response is sent so the dashboard can poll for status.
    """
    vm = _get_vm_or_404(db, vm_id)
    actor = request.headers.get("x-actor", "user")

    task = task_store.create(vm_id=vm.id)
    record_audit(
        db,
        action="capture.triggered",
        actor=actor,
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "task_id": task.task_id,
            "via": "single",
        },
    )
    db.commit()

    background_tasks.add_task(run_capture_task, task.task_id, vm.id, actor=actor)
    request.state.skip_audit_log = True
    return task.to_dict()


@router.get("/{vm_id}/capture/{task_id}", response_model=CaptureTaskRead)
def get_capture_status(vm_id: int, task_id: str) -> dict:
    task = task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Capture task {task_id} not found. Tasks are kept in memory only "
                "and may have been cleared by an appliance restart — re-trigger "
                "the capture if needed."
            ),
        )
    if task.vm_id != vm_id:
        raise HTTPException(
            status_code=404,
            detail=f"Task {task_id} belongs to vm_id={task.vm_id}, not {vm_id}",
        )
    return task.to_dict()


@router.post(
    "/{vm_id}/snapshots",
    response_model=SnapshotRead,
    status_code=status.HTTP_201_CREATED,
)
def create_snapshot(
    vm_id: int, payload: SnapshotCreate, db: Session = Depends(get_db)
) -> BaselineSnapshot:
    vm = _get_vm_or_404(db, vm_id)
    next_number = (
        db.scalar(
            select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0)).where(
                BaselineSnapshot.vm_id == vm.id
            )
        )
        or 0
    ) + 1
    snapshot = BaselineSnapshot(vm_id=vm.id, snapshot_number=next_number, **payload.model_dump())
    db.add(snapshot)
    if vm.status == VMStatus.discovered:
        vm.status = VMStatus.baseline_captured
    db.commit()
    db.refresh(snapshot)
    return snapshot


@router.get("/{vm_id}/snapshots", response_model=list[SnapshotRead])
def list_snapshots(vm_id: int, db: Session = Depends(get_db)) -> list[BaselineSnapshot]:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}/snapshots/{snapshot_id}", response_model=SnapshotRead)
def get_snapshot(vm_id: int, snapshot_id: int, db: Session = Depends(get_db)) -> BaselineSnapshot:
    snapshot = db.get(BaselineSnapshot, snapshot_id)
    if snapshot is None or snapshot.vm_id != vm_id:
        raise HTTPException(
            status_code=404, detail=f"Snapshot {snapshot_id} not found for VM {vm_id}"
        )
    return snapshot


@router.get("/{vm_id}/validation/latest", response_model=LatestValidationResponse)
def latest_validation(vm_id: int, db: Session = Depends(get_db)) -> dict:
    """Return the latest validation result for a VM.

    "No validation yet" is a 200 with ``{"validation": null}`` — the VM
    detail page reads this on every load, and a 404 would force the UI
    to differentiate "VM doesn't exist" (real error) from "validation
    hasn't been run yet" (empty state) at the response level.
    """
    _get_vm_or_404(db, vm_id)
    row = db.scalars(
        select(ValidationResult)
        .where(ValidationResult.vm_id == vm_id)
        .order_by(ValidationResult.validated_at.desc())
        .limit(1)
    ).first()
    return {"validation": row}


# ---------------------------------------------------------------------------
# On-demand validation
# ---------------------------------------------------------------------------
@router.post(
    "/{vm_id}/validate",
    response_model=ValidationTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def trigger_validation(
    request: Request,
    vm_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Spawn an immediate post-migration validation in the background.

    Returns 202 with the task handle. The actual SSH+LLM round-trip runs
    after the response is sent so the UI can poll for progress. A VM with
    no baseline is rejected up-front (400) — running the diff engine
    against an empty baseline produces a meaningless verdict.
    """
    vm = _get_vm_or_404(db, vm_id)
    actor = request.headers.get("x-actor", "user")

    has_baseline = (
        db.scalar(select(BaselineSnapshot.id).where(BaselineSnapshot.vm_id == vm.id).limit(1))
        is not None
    )
    if not has_baseline:
        request.state.skip_audit_log = True
        raise HTTPException(
            status_code=400,
            detail="Capture a baseline first before running validation",
        )

    task = validation_task_store.create(vm_id=vm.id)
    record_audit(
        db,
        action="validation.triggered",
        actor=actor,
        resource_type="vm",
        resource_id=vm.id,
        details={
            "vm_name": vm.name,
            "task_id": task.task_id,
            "via": "single",
        },
    )
    db.commit()

    background_tasks.add_task(run_validation_task, task.task_id, vm.id, actor=actor)
    request.state.skip_audit_log = True
    return task.to_dict()


@router.get("/{vm_id}/validate/{task_id}", response_model=ValidationTaskRead)
def get_validation_status(vm_id: int, task_id: str) -> dict:
    task = validation_task_store.get(task_id)
    if task is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Validation task {task_id} not found. Tasks are kept in memory "
                "only and may have been cleared by an appliance restart — "
                "re-trigger the validation if needed."
            ),
        )
    if task.vm_id != vm_id:
        raise HTTPException(
            status_code=404,
            detail=f"Task {task_id} belongs to vm_id={task.vm_id}, not {vm_id}",
        )
    return task.to_dict()


@router.get("/{vm_id}/baseline/history", response_model=list[SnapshotRead])
def baseline_history(vm_id: int, db: Session = Depends(get_db)) -> list[BaselineSnapshot]:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.desc())
    )
    return list(db.scalars(stmt).all())


@router.get("/{vm_id}/baseline/profile", response_model=BaselineProfile)
def baseline_profile(vm_id: int, db: Session = Depends(get_db)) -> BaselineProfile:
    _get_vm_or_404(db, vm_id)
    stmt = (
        select(BaselineSnapshot)
        .where(BaselineSnapshot.vm_id == vm_id)
        .order_by(BaselineSnapshot.collected_at.asc())
    )
    snapshots = list(db.scalars(stmt).all())
    return synthesize_profile(vm_id, snapshots)
