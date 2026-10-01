"""Plan membership: who is in which wave, and operator edits to it.

Mounted at ``/api/plans`` next to ``plans.py`` and ``waves.py``:

  - ``GET  /{plan_id}/vms``           every VM of the plan with its wave,
                                      live from inventory
  - ``GET  /{plan_id}/edit-targets``  the other plans VMs may move into
  - ``POST /{plan_id}/edits``         move / remove / add VMs (or dry-run)
  - ``POST /{plan_id}/waves/{n}/reannotate``  re-queue one wave's LLM
                                      rationale

The edit logic itself is ``app.core.plan_edit``; this module is HTTP
shape, locking and the background hand-off.
"""

from __future__ import annotations

import logging
from collections import Counter

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.collection.wave_jobs import latest_baseline_coverage
from app.core.config import settings
from app.core.db import get_db
from app.core.family import detect_family
from app.core.plan_edit import (
    EditSpec,
    PlanEditError,
    PreparedEdit,
    apply,
    prepare,
    wave_membership,
)
from app.core.plan_membership import plan_partitions
from app.core.plan_reannotation import queue_wave_reannotation, reannotate_waves_task
from app.core.preclassifier import detect_role
from app.core.wave_skeleton import MAX_VMS_PER_WAVE
from app.models.baseline_run import BaselineRun, BaselineRunStatus
from app.models.plan import MigrationPlan
from app.models.validation_run import ValidationRun, ValidationRunStatus
from app.models.vm import VM
from app.schemas.plan_edit import (
    EditTargetsResponse,
    PlanEditRequest,
    PlanEditResponse,
    PlanVMsResponse,
    ReannotateAccepted,
)

logger = logging.getLogger(__name__)

plan_edits_router = APIRouter(tags=["plans"])


def _get_plan_or_404(db: Session, plan_id: int) -> MigrationPlan:
    plan = db.get(MigrationPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    return plan


def _enum_value(value) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)


# ---------------------------------------------------------------------------
# Read: the plan's VMs
# ---------------------------------------------------------------------------
@plan_edits_router.get("/{plan_id}/vms", response_model=PlanVMsResponse)
def list_plan_vms(plan_id: int, db: Session = Depends(get_db)) -> dict:
    """Every VM in the plan with the wave it is in.

    Membership comes from the plan's waves; the per-VM facts are read
    live from inventory, so a rename or a re-import shows up here without
    regenerating the plan. Kept off ``GET /api/plans/{id}`` because that
    payload is polled every few seconds during generation.

    Returns 200 with empty ``items`` for a plan that has no waves yet.
    """
    plan = _get_plan_or_404(db, plan_id)
    members = wave_membership(plan)
    stored_names: dict[int, str] = {}
    for w in plan.waves or []:
        if isinstance(w, dict):
            for i, vid in enumerate(w.get("vm_ids") or []):
                names = w.get("vm_names") or []
                if i < len(names):
                    stored_names[int(vid)] = names[i]

    all_ids = [vid for ids in members.values() for vid in ids]
    rows = {}
    if all_ids:
        rows = {
            r.id: r
            for r in db.execute(
                select(
                    VM.id,
                    VM.name,
                    VM.role,
                    VM.environment,
                    VM.ip_address,
                    VM.os_family,
                    VM.application_hint,
                    VM.assessment_status,
                    VM.lifecycle_state,
                ).where(VM.id.in_(all_ids))
            ).all()
        }
    family_sizes = Counter(detect_family(r.name) for r in rows.values())

    wave_numbers = sorted(members)
    coverage = latest_baseline_coverage(db, plan.id, wave_numbers) if wave_numbers else {}
    active: set[int] = set()
    validated: set[int] = set()
    if wave_numbers:
        active.update(
            db.scalars(
                select(BaselineRun.wave_number).where(
                    BaselineRun.plan_id == plan.id,
                    BaselineRun.status.in_((BaselineRunStatus.pending, BaselineRunStatus.running)),
                )
            ).all()
        )
        for wave_number, run_status in db.execute(
            select(ValidationRun.wave_number, ValidationRun.status).where(
                ValidationRun.plan_id == plan.id
            )
        ).all():
            if run_status in (ValidationRunStatus.pending, ValidationRunStatus.running):
                active.add(wave_number)
            elif run_status == ValidationRunStatus.completed:
                validated.add(wave_number)

    items: list[dict] = []
    waves: list[dict] = []
    for number in wave_numbers:
        run_and_captured = coverage.get(number)
        captured = run_and_captured[1] if run_and_captured else None
        missing_baseline: list[int] = []
        for vid in members[number]:
            row = rows.get(vid)
            covered = None if captured is None else vid in captured
            if covered is False:
                missing_baseline.append(vid)
            if row is None:
                items.append(
                    {
                        "vm_id": vid,
                        "name": stored_names.get(vid, f"vm-{vid}"),
                        "wave_number": number,
                        "baseline_covered": covered,
                        "missing": True,
                    }
                )
                continue
            family = detect_family(row.name)
            in_family = family_sizes[family] >= 2
            items.append(
                {
                    "vm_id": vid,
                    "name": row.name,
                    "wave_number": number,
                    "environment": row.environment,
                    "ip_address": row.ip_address,
                    "os_family": row.os_family,
                    "application_hint": row.application_hint,
                    "role": detect_role(row),
                    "ha_family": family if in_family else None,
                    "ha_family_size": family_sizes[family] if in_family else None,
                    "assessment_status": row.assessment_status,
                    "lifecycle_state": _enum_value(row.lifecycle_state),
                    "baseline_covered": covered,
                    "missing": False,
                }
            )
        run = run_and_captured[0] if run_and_captured else None
        waves.append(
            {
                "wave_number": number,
                "vm_count": len(members[number]),
                "baseline": {
                    "last_run_id": run.id if run else None,
                    "last_completed_at": (
                        run.completed_at.isoformat() if run and run.completed_at else None
                    ),
                    "missing_vm_ids": missing_baseline,
                },
                "active_run": number in active,
                "validated": number in validated,
            }
        )

    partition = plan_partitions(db, [plan])[plan.id]
    return {
        "plan_id": plan.id,
        "status": plan.status,
        "editable": plan.status == "complete",
        "partition": partition.to_dict() if partition else None,
        "limits": {
            "max_vms_per_wave": MAX_VMS_PER_WAVE,
            "max_vms_per_plan": settings.max_vms_per_plan,
        },
        "items": items,
        "total": len(items),
        "waves": waves,
    }


# ---------------------------------------------------------------------------
# Read: where VMs of this plan may move
# ---------------------------------------------------------------------------
@plan_edits_router.get("/{plan_id}/edit-targets", response_model=EditTargetsResponse)
def list_edit_targets(plan_id: int, db: Session = Depends(get_db)) -> dict:
    """Other plans this plan's VMs can be moved into: status ``complete``
    and the same ``(vCenter, cluster, namespace)`` partition. Same-series
    batches come first. ``ineligible`` says why every other plan is not
    offered, so "why can't I pick plan X?" has an answer on screen.
    """
    plan = _get_plan_or_404(db, plan_id)
    others = list(
        db.scalars(
            select(MigrationPlan)
            .where(MigrationPlan.id != plan.id)
            .order_by(MigrationPlan.created_at.desc(), MigrationPlan.series_index.asc())
            .limit(500)
        ).all()
    )
    partitions = plan_partitions(db, [plan, *others])
    mine = partitions.get(plan.id)

    targets: list[dict] = []
    ineligible: list[dict] = []
    for other in others:
        if other.status == "failed":
            continue  # holds no VMs; not a destination anyone is looking for
        theirs = partitions.get(other.id)
        if other.status != "complete":
            reason = (
                "already marked migrated"
                if other.status == "migrated"
                else f"still generating ({other.status})"
            )
            ineligible.append({"plan_id": other.id, "name": other.name, "reason": reason})
            continue
        if mine is None or theirs is None or mine.key != theirs.key:
            where = theirs.label() if theirs else "an unresolved target"
            ineligible.append(
                {"plan_id": other.id, "name": other.name, "reason": f"migrates to {where}"}
            )
            continue
        targets.append(
            {
                "plan_id": other.id,
                "name": other.name,
                "same_series": bool(plan.series_id) and other.series_id == plan.series_id,
                "series_index": other.series_index,
                "series_total": other.series_total,
                "migration_type": other.migration_type or "cold",
                "vm_count": len(other.vm_ids or []),
                "waves": [
                    {
                        "wave_number": w.get("wave_number"),
                        "vm_count": len(w.get("vm_ids") or []),
                        "estimated_risk": w.get("estimated_risk"),
                    }
                    for w in (other.waves or [])
                    if isinstance(w, dict) and w.get("wave_number") is not None
                ],
            }
        )
    targets.sort(key=lambda t: (not t["same_series"], t["series_index"] or 0))
    return {
        "partition": mine.to_dict() if mine else None,
        "targets": targets,
        "ineligible": ineligible,
    }


# ---------------------------------------------------------------------------
# Write: the edit
# ---------------------------------------------------------------------------
def _response(prepared: PreparedEdit, *, applied: bool, dry_run: bool, reannotation: str) -> dict:
    return {
        "applied": applied,
        "dry_run": dry_run,
        "operation": prepared.spec.operation,
        "requires_confirmation": bool(prepared.overridable) and not prepared.blocking,
        "violations": [v.to_dict() for v in prepared.violations],
        "moves": prepared.moves,
        "plans": [c.summary() for c in prepared.changes.values()],
        "reannotation": reannotation,
    }


def _raise_edit_error(exc: PlanEditError) -> None:
    detail: dict | str = exc.message
    if exc.violations:
        detail = {
            "code": exc.code,
            "message": exc.message,
            "violations": [v.to_dict() for v in exc.violations],
        }
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


@plan_edits_router.post("/{plan_id}/edits", response_model=PlanEditResponse)
def edit_plan(
    request: Request,
    plan_id: int,
    payload: PlanEditRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Move VMs between waves or plans, remove them, or add available VMs.

    One operation over many VMs, atomic. With ``dry_run`` nothing is
    written and the response (always 200) lists what would move and every
    rule the edit would break — the UI calls this first and shows the
    result before committing.

    On commit:

      - 409 — a plan involved is not ``complete``, or the edit breaks an
        ``overridable`` rule and ``confirm_overrides`` was not sent.
      - 422 — the edit breaks a ``blocking`` rule (cross-partition move,
        mapping gap, a run in progress, …) or is malformed.

    The touched waves are rebuilt deterministically in this request and
    handed to the LLM for a rationale in the background.
    """
    actor = request.headers.get("x-actor", "user")
    spec = EditSpec(
        operation=payload.operation,
        vm_ids=list(payload.vm_ids),
        target_wave=payload.target_wave if isinstance(payload.target_wave, int) else None,
        new_wave=payload.target_wave == "new",
        target_plan_id=payload.target_plan_id,
        note=(payload.note or "").strip() or None,
    )
    if payload.dry_run:
        # A dry run changes nothing; it must not look like a change in
        # the audit trail either.
        request.state.skip_audit_log = True
        plan = _get_plan_or_404(db, plan_id)
        try:
            prepared = prepare(db, plan, spec, actor=actor)
        except PlanEditError as exc:
            _raise_edit_error(exc)
        db.rollback()
        return _response(prepared, applied=False, dry_run=True, reannotation="none")

    # Lock every plan row the edit can touch, in id order, then evaluate
    # under the lock so a concurrent edit cannot interleave.
    lock_ids = sorted({plan_id, payload.target_plan_id or plan_id})
    locked = {
        p.id: p
        for p in db.scalars(
            select(MigrationPlan)
            .where(MigrationPlan.id.in_(lock_ids))
            .order_by(MigrationPlan.id.asc())
            .with_for_update()
        ).all()
    }
    plan = locked.get(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    try:
        prepared = prepare(db, plan, spec, actor=actor)
        if prepared.blocking:
            raise PlanEditError(
                422,
                "blocked",
                "This change is not possible: " + " ".join(v.message for v in prepared.blocking),
                prepared.violations,
            )
        if prepared.overridable and not payload.confirm_overrides:
            raise PlanEditError(
                409,
                "confirmation_required",
                "This change overrides a planner rule; resend with confirm_overrides=true "
                "to apply it: " + " ".join(v.message for v in prepared.overridable),
                prepared.violations,
            )
        apply(db, prepared, actor=actor)
    except PlanEditError as exc:
        db.rollback()
        _raise_edit_error(exc)
    db.commit()
    # apply() wrote the specific plan.edit.* rows; skip the generic one.
    request.state.skip_audit_log = True

    queued = False
    for change in prepared.changes.values():
        tokens = change.reannotation_tokens()
        if tokens:
            background_tasks.add_task(reannotate_waves_task, change.plan.id, tokens, actor=actor)
            queued = True
    return _response(
        prepared, applied=True, dry_run=False, reannotation="queued" if queued else "none"
    )


@plan_edits_router.post(
    "/{plan_id}/waves/{wave_number}/reannotate",
    response_model=ReannotateAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
def reannotate_wave(
    request: Request,
    plan_id: int,
    wave_number: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> dict:
    """Ask the LLM for a fresh rationale for one wave. The recovery path
    when a background re-annotation was lost (appliance restart) or fell
    back to deterministic text."""
    plan = db.get(MigrationPlan, plan_id, with_for_update=True)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Plan {plan_id} not found")
    if plan.status != "complete":
        raise HTTPException(
            status_code=409,
            detail=f"Plan {plan_id} is in status {plan.status!r}; only 'complete' plans "
            "can be re-annotated.",
        )
    token = queue_wave_reannotation(plan, wave_number)
    if token is None:
        raise HTTPException(
            status_code=404, detail=f"Plan {plan_id} has no wave numbered {wave_number}"
        )
    db.commit()
    background_tasks.add_task(
        reannotate_waves_task,
        plan_id,
        {wave_number: token},
        actor=request.headers.get("x-actor", "user"),
    )
    return {"plan_id": plan_id, "wave_number": wave_number, "reannotation": "queued"}
