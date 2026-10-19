"""Manual edits to a generated migration plan.

The planner decides wave structure from what the estate data shows.
Operators know things it does not — an undocumented dependency, a
maintenance window, a team that wants two apps cut over together. This
module lets them rearrange a ``complete`` plan:

  - ``move_to_wave``  move VMs to another wave of the same plan, or
                      into a new wave
  - ``move_to_plan``  move VMs into a wave of another plan in the same
                      ``(vCenter, cluster, namespace)`` partition
  - ``remove``        drop VMs from the plan (they become ``available``)
  - ``add``           bring ``available`` inventory VMs into a wave

One call is one operation over many VMs and is atomic. :func:`prepare`
computes the outcome without writing anything — it IS the dry run — and
:func:`apply` persists a prepared edit.

Rules
-----
  - **Wave numbers are identity.** Baseline / validation runs and MTV CR
    names (``vv-plan-{id}-wave-{n}``) key on the wave number, so an edit
    never renumbers. An emptied wave is dropped; a new wave is numbered
    past ``MigrationPlan.wave_number_seq`` so a dropped number is never
    reused.
  - **Partition coherence is not negotiable.** A wave is one MTV Plan
    CR: one source provider, one destination provider, one namespace.
    Cross-partition moves are ``blocking``.
  - **Planner limits are advice.** Wave size and HA family spread are
    ``overridable``: reported with specifics, applied only with
    ``confirm_overrides``, audited, and recorded on the wave.
  - **Only the edit's own damage is reported.** Rule checks diff the
    layout before and after (``wave_constraints.diff_violations``).

Every touched wave is rebuilt the way Stage 7 builds a generated one
(classify → ``build_wave`` → considerations → YAML emit) with a
deterministic annotation, ``method="mechanical_interim"``. The LLM then
re-explains the touched waves in the background
(``app.core.plan_reannotation``); the interim text is the Python-only
path and is always already persisted.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.assessment import blocks_migration_type
from app.core.audit import record_audit
from app.core.concurrency import assign_concurrency_groups
from app.core.config import settings
from app.core.mapping_catalog import CatalogSnapshot, load_catalogs_for
from app.core.mapping_checks import wave_mapping_view
from app.core.mapping_validation import validate_plan_inputs
from app.core.mtv import MTVGenerationError
from app.core.plan_membership import plan_partitions, resolve_partitions
from app.core.plan_pipeline import (
    AnnotatedWave,
    build_resolver,
    emit_wave_yaml,
    mapping_for_wave,
    wave_considerations,
)
from app.core.preclassifier import GroupKey, PreClassifier, VMGroup
from app.core.vm_lifecycle import (
    LifecycleTransitionError,
    record_plan_reassignment,
    transition_to_available_from_plan_edit,
    transition_to_planned,
)
from app.core.wave_annotation import (
    ManualWaveContext,
    _mechanical_annotation,
    override_line,
    reduce_groups_for_annotation,
)
from app.core.wave_constraints import (
    BLOCKING,
    INFO,
    OVERRIDABLE,
    Violation,
    WaveMembers,
    check_waves,
    diff_violations,
)
from app.core.wave_skeleton import Wave, build_wave
from app.models.baseline_run import BaselineRun, BaselineRunStatus
from app.models.plan import MigrationPlan
from app.models.target import ResourceMapping
from app.models.validation_run import ValidationRun, ValidationRunStatus
from app.models.vm import VM, VMLifecycleState

logger = logging.getLogger(__name__)

OPERATIONS = ("move_to_wave", "move_to_plan", "remove", "add")

#: ``method`` on a wave between an edit and its background LLM pass.
METHOD_INTERIM = "mechanical_interim"

# Rule codes recorded on the wave as acknowledged overrides.
_WAVE_RULE_CODES = frozenset({"wave_size", "ha_family_cap"})

_AUDIT_ACTION = {
    "move_to_wave": "plan.edit.move_vm",
    "move_to_plan": "plan.edit.move_to_plan",
    "remove": "plan.edit.remove_vm",
    "add": "plan.edit.add_vm",
}
_AUDIT_LIST_CAP = 200


@dataclass
class EditSpec:
    """One requested edit. ``target_wave`` is an existing wave number;
    ``new_wave`` asks for a fresh wave instead."""

    operation: str
    vm_ids: list[int]
    target_wave: int | None = None
    new_wave: bool = False
    target_plan_id: int | None = None
    note: str | None = None


class PlanEditError(Exception):
    """The edit cannot be evaluated or applied. ``status_code`` is the
    HTTP status the API returns; ``violations`` carries the structured
    reasons when there are any."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        violations: list[Violation] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.violations = violations or []


@dataclass
class PlanChange:
    """What one plan row looks like after the edit."""

    plan: MigrationPlan
    waves: list[dict]
    vm_ids: list[int]
    wave_number_seq: int
    touched_waves: list[int] = field(default_factory=list)
    created_waves: list[int] = field(default_factory=list)
    removed_waves: list[int] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan.id,
            "name": self.plan.name,
            "vm_count": len(self.vm_ids),
            "wave_count": len(self.waves),
            "touched_waves": list(self.touched_waves),
            "created_waves": list(self.created_waves),
            "removed_waves": list(self.removed_waves),
        }

    def reannotation_tokens(self) -> dict[int, str]:
        """``{wave_number: token}`` for the waves the LLM should re-explain."""
        out: dict[int, str] = {}
        for doc in self.waves:
            if doc.get("wave_number") in self.touched_waves:
                token = (doc.get("reannotation") or {}).get("token")
                if token:
                    out[int(doc["wave_number"])] = token
        return out


@dataclass
class PreparedEdit:
    spec: EditSpec
    source_plan: MigrationPlan
    target_plan: MigrationPlan
    violations: list[Violation]
    moves: list[dict[str, Any]]
    changes: dict[int, PlanChange]

    @property
    def blocking(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == BLOCKING]

    @property
    def overridable(self) -> list[Violation]:
        return [v for v in self.violations if v.severity == OVERRIDABLE]


# ---------------------------------------------------------------------------
# Membership helpers
# ---------------------------------------------------------------------------
def wave_membership(plan: MigrationPlan) -> dict[int, list[int]]:
    """``{wave_number: [vm_id, ...]}`` from the persisted wave documents."""
    out: dict[int, list[int]] = {}
    for w in plan.waves or []:
        if isinstance(w, dict) and w.get("wave_number") is not None:
            out[int(w["wave_number"])] = [int(v) for v in (w.get("vm_ids") or [])]
    return out


def next_wave_number(plan: MigrationPlan, members: dict[int, list[int]] | None = None) -> int:
    """The number a new wave gets: past every number this plan ever used."""
    current = members if members is not None else wave_membership(plan)
    return max([plan.wave_number_seq or 0, *current.keys()], default=0) + 1


def _stored_names(plan: MigrationPlan) -> dict[int, str]:
    """Names as persisted on the waves — the fallback for a VM whose
    inventory row has since been deleted."""
    out: dict[int, str] = {}
    for w in plan.waves or []:
        if not isinstance(w, dict):
            continue
        ids = w.get("vm_ids") or []
        names = w.get("vm_names") or []
        for i, vid in enumerate(ids):
            if i < len(names) and names[i]:
                out[int(vid)] = names[i]
    return out


def _as_members(waves: dict[int, list[int]], names: dict[int, str]) -> WaveMembers:
    return {n: [(vid, names.get(vid, f"vm-{vid}")) for vid in ids] for n, ids in waves.items()}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lifecycle(vm: VM) -> VMLifecycleState:
    state = vm.lifecycle_state
    return state if isinstance(state, VMLifecycleState) else VMLifecycleState(state)


# ---------------------------------------------------------------------------
# Violations that need the database
# ---------------------------------------------------------------------------
def _run_violations(db: Session, plan_id: int, wave_numbers: set[int]) -> list[Violation]:
    """A wave with a baseline/validation in flight cannot change under
    the collector; a wave that was already validated can, with a warning."""
    if not wave_numbers:
        return []
    out: list[Violation] = []
    active_states_b = (BaselineRunStatus.pending, BaselineRunStatus.running)
    active_states_v = (ValidationRunStatus.pending, ValidationRunStatus.running)
    busy: set[int] = set()
    for wave_number in db.scalars(
        select(BaselineRun.wave_number).where(
            BaselineRun.plan_id == plan_id,
            BaselineRun.wave_number.in_(wave_numbers),
            BaselineRun.status.in_(active_states_b),
        )
    ).all():
        busy.add(wave_number)
    for wave_number in db.scalars(
        select(ValidationRun.wave_number).where(
            ValidationRun.plan_id == plan_id,
            ValidationRun.wave_number.in_(wave_numbers),
            ValidationRun.status.in_(active_states_v),
        )
    ).all():
        busy.add(wave_number)
    for n in sorted(busy):
        out.append(
            Violation(
                code="wave_run_in_progress",
                severity=BLOCKING,
                message=(
                    f"Wave {n} has a baseline or validation run in progress. "
                    "Wait for it to finish before changing the wave."
                ),
                plan_id=plan_id,
                wave_number=n,
            )
        )
    validated = set(
        db.scalars(
            select(ValidationRun.wave_number).where(
                ValidationRun.plan_id == plan_id,
                ValidationRun.wave_number.in_(wave_numbers),
                ValidationRun.status == ValidationRunStatus.completed,
            )
        ).all()
    )
    for n in sorted(validated - busy):
        out.append(
            Violation(
                code="wave_already_validated",
                severity=OVERRIDABLE,
                message=(
                    f"Wave {n} already has a completed validation run. Changing its "
                    "membership leaves that run describing a different set of VMs."
                ),
                plan_id=plan_id,
                wave_number=n,
            )
        )
    return out


def _has_completed_baseline(db: Session, plan_id: int, wave_number: int) -> bool:
    return (
        db.scalar(
            select(BaselineRun.id)
            .where(
                BaselineRun.plan_id == plan_id,
                BaselineRun.wave_number == wave_number,
                BaselineRun.status == BaselineRunStatus.completed,
            )
            .limit(1)
        )
        is not None
    )


def _target_violations(
    db: Session,
    spec: EditSpec,
    source_plan: MigrationPlan,
    target_plan: MigrationPlan,
    vms: list[VM],
) -> list[Violation]:
    """Checks for VMs ENTERING a plan (``move_to_plan`` / ``add``): the
    same gates plan generation applies — target resolves and matches the
    plan's partition, Stage 0 mapping coverage, migratability."""
    out: list[Violation] = []
    pid = target_plan.id

    if spec.operation == "add":
        unavailable = [vm for vm in vms if _lifecycle(vm) != VMLifecycleState.available]
        if unavailable:
            out.append(
                Violation(
                    code="vm_not_available",
                    severity=BLOCKING,
                    message=(
                        f"{len(unavailable)} VM(s) are not available to plan (already in a "
                        "plan, migrated, or unmanageable): "
                        + ", ".join(vm.name for vm in unavailable[:6])
                        + ("…" if len(unavailable) > 6 else "")
                    ),
                    plan_id=pid,
                    vm_ids=tuple(vm.id for vm in unavailable),
                    vm_names=tuple(vm.name for vm in unavailable),
                )
            )

    target_partition = plan_partitions(db, [target_plan])[pid]
    if target_partition is None:
        out.append(
            Violation(
                code="target_unresolved",
                severity=BLOCKING,
                message=(
                    f"Plan '{target_plan.name}' no longer resolves to a target cluster and "
                    "namespace, so VMs cannot be placed into it."
                ),
                plan_id=pid,
            )
        )
        return out

    partitions = resolve_partitions(db, vms)
    unresolved = [vm for vm in vms if partitions.get(vm.id) is None]
    if unresolved:
        out.append(
            Violation(
                code="target_unresolved",
                severity=BLOCKING,
                message=(
                    f"{len(unresolved)} VM(s) do not resolve to a target cluster and namespace: "
                    + ", ".join(vm.name for vm in unresolved[:6])
                    + ("…" if len(unresolved) > 6 else "")
                    + ". Set a target override or add a resource mapping."
                ),
                plan_id=pid,
                vm_ids=tuple(vm.id for vm in unresolved),
                vm_names=tuple(vm.name for vm in unresolved),
            )
        )
    mismatched = [
        vm
        for vm in vms
        if partitions.get(vm.id) is not None and partitions[vm.id].key != target_partition.key
    ]
    if mismatched:
        out.append(
            Violation(
                code="partition_mismatch",
                severity=BLOCKING,
                message=(
                    f"{len(mismatched)} VM(s) migrate to a different vCenter, cluster or "
                    f"namespace than plan '{target_plan.name}' ({target_partition.label()}): "
                    + ", ".join(f"{vm.name} ({partitions[vm.id].label()})" for vm in mismatched[:4])
                    + ("…" if len(mismatched) > 4 else "")
                    + ". A wave is one MTV Plan and cannot span them."
                ),
                plan_id=pid,
                vm_ids=tuple(vm.id for vm in mismatched),
                vm_names=tuple(vm.name for vm in mismatched),
            )
        )

    bad = {vm.id for vm in unresolved} | {vm.id for vm in mismatched}
    eligible = [vm for vm in vms if vm.id not in bad]
    if eligible:
        mappings = _plan_mappings(db, target_plan)
        coverage = validate_plan_inputs(eligible, mappings)
        gaps_by_vm: dict[int, list[str]] = {}
        for g in coverage.gaps:
            gaps_by_vm.setdefault(g.vm_id, []).append(f"{g.kind} '{g.source_value}'")
        for vm in eligible:
            if vm.id in gaps_by_vm:
                out.append(
                    Violation(
                        code="mapping_gap",
                        severity=BLOCKING,
                        message=(
                            f"{vm.name}: plan '{target_plan.name}' has no mapping for "
                            + ", ".join(gaps_by_vm[vm.id])
                        ),
                        plan_id=pid,
                        vm_ids=(vm.id,),
                        vm_names=(vm.name,),
                    )
                )

    # A VM already in a plan of the same type was admitted (possibly with
    # ``override_assessment``) when that plan was generated; re-checking
    # it would block a move the operator already vouched for.
    recheck = spec.operation == "add" or (
        (source_plan.migration_type or "cold") != (target_plan.migration_type or "cold")
    )
    if recheck:
        migration_type = target_plan.migration_type or "cold"
        for vm in vms:
            ids = blocks_migration_type(vm.assessment, migration_type)
            if ids:
                out.append(
                    Violation(
                        code="assessment_blocked",
                        severity=BLOCKING,
                        message=(
                            f"{vm.name} cannot migrate as a {migration_type} plan: "
                            + ", ".join(ids)
                        ),
                        plan_id=pid,
                        vm_ids=(vm.id,),
                        vm_names=(vm.name,),
                    )
                )
    return out


def _plan_mappings(db: Session, plan: MigrationPlan) -> list[ResourceMapping]:
    ids = list(plan.mapping_ids or [])
    if not ids:
        return []
    return list(db.scalars(select(ResourceMapping).where(ResourceMapping.id.in_(ids))).all())


# ---------------------------------------------------------------------------
# Rebuild
# ---------------------------------------------------------------------------
def _override_entries(
    plan: MigrationPlan,
    after: WaveMembers,
    introduced: list[Violation],
    actor: str,
) -> dict[int, list[dict[str, Any]]]:
    """Acknowledged rule overrides per wave after this edit: the ones
    recorded earlier that still hold, plus the ones this edit introduces.
    A rule the layout no longer breaks drops off the wave."""
    standing = {
        (v.wave_number, v.code, v.family): v
        for v in check_waves(after, plan_id=plan.id)
        if v.code in _WAVE_RULE_CODES
    }
    out: dict[int, list[dict[str, Any]]] = {}
    seen: set[tuple] = set()
    for w in plan.waves or []:
        if not isinstance(w, dict):
            continue
        number = w.get("wave_number")
        for entry in w.get("overrides") or []:
            key = (number, entry.get("code"), entry.get("family"))
            current = standing.get(key)
            if current is None:
                continue
            seen.add(key)
            out.setdefault(number, []).append(
                {**entry, "count": current.count, "limit": current.limit}
            )
    now = _now_iso()
    for v in introduced:
        if v.code not in _WAVE_RULE_CODES or v.plan_id != plan.id:
            continue
        key = (v.wave_number, v.code, v.family)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(v.wave_number, []).append(
            {
                "code": v.code,
                "family": v.family,
                "count": v.count,
                "limit": v.limit,
                "by": actor,
                "at": now,
            }
        )
    return out


def classify_wave(wave_number: int, vms: list[VM]) -> tuple[Wave, Wave]:
    """Rebuild a hand-arranged wave from its VMs.

    Returns ``(wave, annotation_view)``: the wave with its natural groups
    (what gets persisted and emitted), and a view whose groups are
    reduced to the per-LLM-call ceiling (what an annotator may see).
    """
    groups = PreClassifier().classify(vms)
    wave = build_wave(wave_number, groups)
    view = Wave(
        wave_number=wave_number,
        groups=reduce_groups_for_annotation(groups),
        estimated_risk=wave.estimated_risk,
    )
    return wave, view


def manual_context(doc: dict[str, Any]) -> ManualWaveContext | None:
    """The annotator's view of a persisted wave's manual-edit state, or
    ``None`` for a wave nobody edited."""
    edit = doc.get("manual_edit")
    if not edit:
        return None
    return ManualWaveContext(
        actor=edit.get("edited_by") or "operator",
        note=edit.get("note"),
        overrides=[
            override_line(o.get("code", ""), o.get("count"), o.get("limit"))
            for o in (doc.get("overrides") or [])
        ],
    )


def _build_wave_doc(
    plan: MigrationPlan,
    wave_number: int,
    vms: list[VM],
    previous: dict[str, Any] | None,
    overrides: list[dict[str, Any]],
    mappings: list[ResourceMapping],
    spec: EditSpec,
    actor: str,
    catalogs: dict[int, CatalogSnapshot] | None = None,
) -> tuple[dict[str, Any], str | None]:
    """One touched wave's persisted document, plus the YAML emit error
    if the rebuilt wave cannot be emitted."""
    previous = previous or {}
    prior_edit = previous.get("manual_edit") or {}
    manual_edit = {
        "edited_at": _now_iso(),
        "edited_by": actor,
        "edit_count": int(prior_edit.get("edit_count") or 0) + 1,
        "note": spec.note or prior_edit.get("note"),
    }
    wave, view = classify_wave(wave_number, vms)
    ann = _mechanical_annotation(
        view, manual_context({"manual_edit": manual_edit, "overrides": overrides})
    )
    vm_by_id = {vm.id: vm for vm in vms}
    aw = AnnotatedWave(
        wave=wave,
        description=ann.description,
        risk_score=ann.risk_score,
        risk_rationale=ann.risk_rationale,
        notable_concerns=list(ann.notable_concerns),
        method=METHOD_INTERIM,
    )
    aw.vm_names = [vm_by_id[vid].name for vid in wave.vm_ids]
    aw.considerations = wave_considerations(
        [vm_by_id[vid] for vid in wave.vm_ids], plan.migration_type or "cold"
    )
    yaml_error: str | None = None
    resolver = build_resolver(mapping_for_wave(wave, mappings, vm_by_id), catalogs)
    aw.mapping_summary, aw.mapping_findings = wave_mapping_view(
        [vm_by_id[vid] for vid in wave.vm_ids], resolver, plan.migration_type or "cold"
    )
    try:
        aw.mtv_yaml = emit_wave_yaml(
            plan.id,
            wave,
            ann.description,
            vm_by_id,
            resolver,
            plan.migration_type or "cold",
        )
    except MTVGenerationError as exc:
        yaml_error = str(exc)
    doc = aw.to_dict()
    doc["manual_edit"] = manual_edit
    doc["overrides"] = overrides
    doc["reannotation"] = {"state": "queued", "token": uuid4().hex, "queued_at": _now_iso()}
    return doc, yaml_error


def _shell_wave(wave_number: int, vm_ids: list[int], vcenter_id: int | None) -> Wave:
    """A wave carrying only what Stage 5 reads: its VM ids and vCenter."""
    return Wave(
        wave_number=wave_number,
        groups=[
            VMGroup(
                key=GroupKey(
                    vcenter_id=vcenter_id,
                    target_namespace="",
                    role="other",
                    state="unknown",
                    discriminator="shell",
                ),
                vm_ids=list(vm_ids),
                shared_attributes={},
                estimated_role="other",
                estimated_state="unknown",
                migration_risk="low",
            )
        ],
    )


def _rebuild_plan(
    db: Session,
    plan: MigrationPlan,
    after: dict[int, list[int]],
    touched: list[int],
    created: list[int],
    removed: list[int],
    overrides: dict[int, list[dict[str, Any]]],
    names: dict[int, str],
    vcenter_by_vm: dict[int, int | None],
    spec: EditSpec,
    actor: str,
) -> tuple[PlanChange, list[Violation]]:
    previous = {
        int(w["wave_number"]): w
        for w in (plan.waves or [])
        if isinstance(w, dict) and w.get("wave_number") is not None
    }
    touched_ids = {vid for n in touched for vid in after[n]}
    vm_rows = (
        {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(touched_ids))).all()}
        if touched_ids
        else {}
    )
    mappings = _plan_mappings(db, plan)
    catalogs = load_catalogs_for(db, mappings)
    violations: list[Violation] = []
    docs: list[dict[str, Any]] = []
    for number in sorted(after):
        if number in touched:
            # A VM whose inventory row was deleted cannot be classified or
            # emitted; it falls out of a rebuilt wave.
            vms = [vm_rows[vid] for vid in after[number] if vid in vm_rows]
            if not vms:
                removed.append(number)
                continue
            doc, yaml_error = _build_wave_doc(
                plan,
                number,
                vms,
                previous.get(number),
                overrides.get(number, []),
                mappings,
                spec,
                actor,
                catalogs,
            )
            if yaml_error:
                was_ok = number in created or bool(
                    (previous.get(number) or {}).get("mtv_yaml_available")
                )
                violations.append(
                    Violation(
                        code="yaml_emit_failed",
                        severity=BLOCKING if was_ok else INFO,
                        message=f"Wave {number} could not be emitted as MTV YAML: {yaml_error}",
                        plan_id=plan.id,
                        wave_number=number,
                    )
                )
        else:
            doc = copy.deepcopy(previous[number])
            # A rule can start or stop holding on a wave the edit did not
            # touch (the family cap follows the plan-wide family size).
            if overrides.get(number) or doc.get("overrides"):
                doc["overrides"] = overrides.get(number, [])
        docs.append(doc)

    # Stage 5 is whole-plan: colour every wave again, in wave-number order.
    shells = [
        _shell_wave(
            d["wave_number"],
            d.get("vm_ids") or [],
            next(
                (
                    vcenter_by_vm[vid]
                    for vid in d.get("vm_ids") or []
                    if vcenter_by_vm.get(vid) is not None
                ),
                None,
            ),
        )
        for d in docs
    ]
    assign_concurrency_groups(shells, names)
    for doc, shell in zip(docs, shells, strict=True):
        doc["concurrency_group_id"] = shell.concurrency_group_id

    vm_ids = [int(vid) for d in docs for vid in (d.get("vm_ids") or [])]
    if len(vm_ids) != len(set(vm_ids)):
        raise PlanEditError(
            500, "membership_corrupt", f"Plan {plan.id}: a VM would appear in two waves."
        )
    kept = {d["wave_number"] for d in docs}
    change = PlanChange(
        plan=plan,
        waves=docs,
        vm_ids=vm_ids,
        wave_number_seq=max([plan.wave_number_seq or 0, *previous.keys(), *after.keys()]),
        touched_waves=sorted(n for n in touched if n in kept),
        created_waves=sorted(n for n in created if n in kept),
        removed_waves=sorted(set(removed)),
    )
    return change, violations


# ---------------------------------------------------------------------------
# prepare / apply
# ---------------------------------------------------------------------------
def prepare(
    db: Session, plan: MigrationPlan, spec: EditSpec, *, actor: str = "user"
) -> PreparedEdit:
    """Work out what ``spec`` would do to ``plan`` (and to the target plan
    for a cross-plan move) without writing anything.

    Raises :class:`PlanEditError` only when the request cannot be
    evaluated at all (wrong plan status, unknown ids, malformed target).
    Everything the operator can act on comes back as ``violations``.
    """
    if spec.operation not in OPERATIONS:
        raise PlanEditError(422, "bad_operation", f"Unknown operation {spec.operation!r}")
    vm_ids = list(dict.fromkeys(int(v) for v in spec.vm_ids))
    if not vm_ids:
        raise PlanEditError(422, "no_vms", "vm_ids must contain at least one VM")
    if plan.status != "complete":
        raise PlanEditError(
            409,
            "plan_not_editable",
            f"Plan {plan.id} is in status {plan.status!r}; only plans in 'complete' can be edited.",
        )

    target_plan = plan
    if spec.operation == "move_to_plan":
        if spec.target_plan_id is None or spec.target_plan_id == plan.id:
            raise PlanEditError(
                422, "bad_target", "move_to_plan needs target_plan_id of a different plan"
            )
        target_plan = db.get(MigrationPlan, spec.target_plan_id)
        if target_plan is None:
            raise PlanEditError(404, "not_found", f"Plan {spec.target_plan_id} not found")
        if target_plan.status != "complete":
            raise PlanEditError(
                409,
                "plan_not_editable",
                f"Plan {target_plan.id} is in status {target_plan.status!r}; VMs can only be "
                "moved into a plan in 'complete'.",
            )
    if spec.operation != "remove" and not spec.new_wave and spec.target_wave is None:
        raise PlanEditError(422, "bad_target", 'target_wave is required (a wave number or "new")')

    vms = list(db.scalars(select(VM).where(VM.id.in_(vm_ids))).all())
    vm_by_id = {vm.id: vm for vm in vms}
    missing = [vid for vid in vm_ids if vid not in vm_by_id]
    if missing:
        raise PlanEditError(404, "not_found", f"Unknown vm_ids: {missing}")

    before: dict[int, dict[int, list[int]]] = {plan.id: wave_membership(plan)}
    if target_plan.id != plan.id:
        before[target_plan.id] = wave_membership(target_plan)
    after = copy.deepcopy(before)
    wave_of = {vid: n for n, ids in before[plan.id].items() for vid in ids}

    if spec.operation == "add":
        present = [vm_by_id[v].name for v in vm_ids if v in wave_of]
        if present:
            raise PlanEditError(
                422, "already_member", f"Already in this plan: {', '.join(present[:8])}"
            )
    else:
        absent = [vm_by_id[v].name for v in vm_ids if v not in wave_of]
        if absent:
            raise PlanEditError(
                422, "not_member", f"Not in plan {plan.id}: {', '.join(absent[:8])}"
            )

    dest: int | None = None
    if spec.operation != "remove":
        target_waves = after[target_plan.id]
        if spec.new_wave:
            dest = next_wave_number(target_plan, target_waves)
        else:
            dest = int(spec.target_wave)
            if dest not in target_waves:
                raise PlanEditError(
                    422, "bad_target", f"Plan {target_plan.id} has no wave numbered {dest}"
                )

    moves: list[dict[str, Any]] = []
    for vid in vm_ids:
        from_wave = wave_of.get(vid)
        if spec.operation == "move_to_wave" and from_wave == dest:
            continue
        if from_wave is not None:
            after[plan.id][from_wave].remove(vid)
        if dest is not None:
            after[target_plan.id].setdefault(dest, []).append(vid)
        moves.append(
            {
                "vm_id": vid,
                "vm_name": vm_by_id[vid].name,
                "from_plan_id": plan.id if from_wave is not None else None,
                "from_wave": from_wave,
                "to_plan_id": target_plan.id if dest is not None else None,
                "to_wave": dest,
            }
        )
    if not moves:
        raise PlanEditError(422, "no_change", f"The selected VMs are already in wave {dest}.")
    moved_vms = [vm_by_id[m["vm_id"]] for m in moves]

    plans = {plan.id: plan, target_plan.id: target_plan}
    layout: dict[int, tuple[list[int], list[int], list[int]]] = {}
    for pid in after:
        removed = [n for n, ids in after[pid].items() if not ids]
        for n in removed:
            del after[pid][n]
        created = [n for n in after[pid] if n not in before[pid]]
        touched = [n for n in after[pid] if n in before[pid] and after[pid][n] != before[pid][n]]
        layout[pid] = (sorted(touched + created), sorted(created), sorted(removed))

    violations: list[Violation] = []
    if not after[plan.id]:
        violations.append(
            Violation(
                code="would_empty_plan",
                severity=BLOCKING,
                message=(
                    f"This would leave plan '{plan.name}' with no VMs. " "Delete the plan instead."
                ),
                plan_id=plan.id,
            )
        )
    for pid, (touched, created, removed) in layout.items():
        existing = (set(touched) - set(created)) | set(removed)
        violations.extend(_run_violations(db, pid, existing))
    if spec.operation in ("move_to_plan", "add"):
        violations.extend(_target_violations(db, spec, plan, target_plan, moved_vms))
        size = sum(len(ids) for ids in after[target_plan.id].values())
        if size > settings.max_vms_per_plan:
            violations.append(
                Violation(
                    code="plan_size",
                    severity=OVERRIDABLE,
                    message=(
                        f"Plan '{target_plan.name}' would hold {size} VMs, above the "
                        f"{settings.max_vms_per_plan}-VM batch size plans are generated at."
                    ),
                    plan_id=target_plan.id,
                    count=size,
                    limit=settings.max_vms_per_plan,
                )
            )

    # Fresh names + vCenters for every VM in the plans involved; the
    # persisted wave names cover rows deleted from inventory since.
    all_ids = {vid for pid in before for ids in before[pid].values() for vid in ids} | set(vm_ids)
    names: dict[int, str] = {}
    for p in plans.values():
        names.update(_stored_names(p))
    vcenter_by_vm: dict[int, int | None] = {}
    for vid, name, vc_id in db.execute(
        select(VM.id, VM.name, VM.source_vcenter_id).where(VM.id.in_(all_ids))
    ).all():
        names[vid] = name
        vcenter_by_vm[vid] = vc_id

    introduced: dict[int, list[Violation]] = {}
    for pid in after:
        introduced[pid] = diff_violations(
            check_waves(_as_members(before[pid], names), plan_id=pid),
            check_waves(_as_members(after[pid], names), plan_id=pid),
        )
        violations.extend(introduced[pid])

    if (
        dest is not None
        and dest in before[target_plan.id]
        and _has_completed_baseline(db, target_plan.id, dest)
    ):
        violations.append(
            Violation(
                code="baseline_not_covered",
                severity=INFO,
                message=(
                    f"Wave {dest} already has a captured baseline. The {len(moves)} VM(s) "
                    "joining it are not in that baseline — capture it again before validating."
                ),
                plan_id=target_plan.id,
                wave_number=dest,
                vm_ids=tuple(m["vm_id"] for m in moves),
                vm_names=tuple(m["vm_name"] for m in moves),
            )
        )

    changes: dict[int, PlanChange] = {}
    if not any(v.severity == BLOCKING for v in violations):
        for pid in after:
            touched, created, removed = layout[pid]
            if not after[pid]:
                continue
            change, rebuild_violations = _rebuild_plan(
                db,
                plans[pid],
                after[pid],
                touched,
                created,
                removed,
                _override_entries(
                    plans[pid], _as_members(after[pid], names), introduced[pid], actor
                ),
                names,
                vcenter_by_vm,
                spec,
                actor,
            )
            changes[pid] = change
            violations.extend(rebuild_violations)

    return PreparedEdit(
        spec=spec,
        source_plan=plan,
        target_plan=target_plan,
        violations=violations,
        moves=moves,
        changes=changes,
    )


def _capped(items: list) -> list:
    return items[:_AUDIT_LIST_CAP]


def apply(db: Session, prepared: PreparedEdit, *, actor: str = "user") -> None:
    """Persist a prepared edit: lifecycle transitions, the rebuilt wave
    documents, and the audit rows. The caller commits.

    Refuses an edit that still has ``blocking`` violations; whether the
    ``overridable`` ones were confirmed is the caller's contract.
    """
    if prepared.blocking:
        raise PlanEditError(
            422, "blocked", "The edit breaks a rule that cannot be overridden.", prepared.blocking
        )
    spec = prepared.spec
    source, target = prepared.source_plan, prepared.target_plan
    moved_ids = [m["vm_id"] for m in prepared.moves]

    try:
        if spec.operation == "add":
            transition_to_planned(moved_ids, target.id, db, actor=actor, trigger="plan.edit.add")
        elif spec.operation == "remove":
            transition_to_available_from_plan_edit(source.id, moved_ids, db, actor=actor)
        elif spec.operation == "move_to_plan":
            record_plan_reassignment(moved_ids, source.id, target.id, db, actor=actor)
    except LifecycleTransitionError as exc:
        raise PlanEditError(422, "lifecycle", str(exc)) from exc

    for change in prepared.changes.values():
        plan = change.plan
        plan.waves = change.waves
        plan.vm_ids = change.vm_ids
        plan.wave_number_seq = change.wave_number_seq
        # JSON columns have no mutation tracking; say so explicitly.
        flag_modified(plan, "waves")
        flag_modified(plan, "vm_ids")

    overridden = [v.to_dict() for v in prepared.overridable]
    action = _AUDIT_ACTION[spec.operation]
    for pid, change in prepared.changes.items():
        details: dict[str, Any] = {
            "operation": spec.operation,
            "vm_ids": _capped(moved_ids),
            "vm_names": _capped([m["vm_name"] for m in prepared.moves]),
            "vm_count": len(moved_ids),
            "moves": _capped(prepared.moves),
            "touched_waves": change.touched_waves,
            "created_waves": change.created_waves,
            "removed_waves": change.removed_waves,
            "overrides": [o for o in overridden if o["plan_id"] == pid],
            "note": spec.note,
        }
        if spec.operation == "move_to_plan":
            details["direction"] = "out" if pid == source.id else "in"
            details["other_plan_id"] = target.id if pid == source.id else source.id
        record_audit(
            db, action=action, actor=actor, resource_type="plan", resource_id=pid, details=details
        )
    if overridden:
        record_audit(
            db,
            action="plan.edit.rule_overridden",
            actor=actor,
            resource_type="plan",
            resource_id=target.id,
            details={
                "operation": spec.operation,
                "rules": _capped(overridden),
                "count": len(overridden),
                "note": spec.note,
            },
        )
    logger.info(
        "plan.edit op=%s plan=%d target_plan=%d vms=%d overrides=%d",
        spec.operation,
        source.id,
        target.id,
        len(moved_ids),
        len(overridden),
    )
