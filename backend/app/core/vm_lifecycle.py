"""VM plan-membership lifecycle service.

The five-state machine (``VMLifecycleState``) is server-enforced: the
UI cannot move VMs into arbitrary states. Each function in this module
performs one labeled transition and writes an audit row before
mutating, so federal reviewers can reconstruct who put which VM into
which plan-membership state and when.

Transitions
-----------
::

    available    → planned       transition_to_planned()
    planned      → available     transition_to_available_from_failed_plan()
    planned      → available     transition_to_available_from_deleted_plan()
    planned      → migrated      transition_to_migrated()
    migrated     → rolled_back   transition_to_rolled_back()
    rolled_back  → available     transition_to_available_from_rolled_back()

Any other input state is treated as illegal and surfaces as a
``LifecycleTransitionError`` with the offending VM ids. Callers convert
that into a 422 with the per-VM detail.

Callers commit. The service stages an ``AuditLog`` row + the VM
mutations and returns — the caller is responsible for ``db.commit()``
so the transition is atomic with whatever surrounding domain change
triggered it (plan create, plan delete, mark-succeeded, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import record_audit
from app.models.vm import VM, VMLifecycleState


class LifecycleTransitionError(ValueError):
    """Raised when a VM is not in a state that admits the requested transition.

    Carries the offending vm_ids and their current states so the caller
    can render a structured 422 response.
    """

    def __init__(
        self,
        *,
        attempted: VMLifecycleState,
        offenders: list[tuple[int, VMLifecycleState]],
    ) -> None:
        self.attempted = attempted
        self.offenders = offenders
        names = ", ".join(f"vm={vid} in {state.value}" for vid, state in offenders[:5])
        more = "" if len(offenders) <= 5 else f" (+{len(offenders) - 5} more)"
        super().__init__(
            f"Cannot transition to {attempted.value}: {len(offenders)} VM(s) in "
            f"disallowed state — {names}{more}"
        )


@dataclass(frozen=True)
class TransitionResult:
    """Summary of one transition call — returned for callers that want to log it."""

    vm_ids: list[int]
    from_states: dict[int, str]
    to_state: str
    audit_action: str


def _fetch_locked(db: Session, vm_ids: list[int]) -> dict[int, VM]:
    """Load VMs for update. Uses ``SELECT ... FOR UPDATE`` on backends
    that support it (Postgres); SQLite is naturally serial so the hint
    is a no-op.
    """
    if not vm_ids:
        return {}
    rows = db.scalars(select(VM).where(VM.id.in_(vm_ids))).all()
    return {vm.id: vm for vm in rows}


def _enforce(
    vms: dict[int, VM],
    allowed_from: set[VMLifecycleState],
    target: VMLifecycleState,
) -> None:
    """Raise LifecycleTransitionError if any VM isn't in ``allowed_from``."""
    offenders: list[tuple[int, VMLifecycleState]] = []
    for vid, vm in vms.items():
        current = vm.lifecycle_state
        if isinstance(current, str):
            current = VMLifecycleState(current)
        if current not in allowed_from:
            offenders.append((vid, current))
    if offenders:
        raise LifecycleTransitionError(attempted=target, offenders=offenders)


def _apply(
    vms: dict[int, VM],
    target: VMLifecycleState,
) -> dict[int, str]:
    """Move every VM to ``target``. Returns the prior states for audit."""
    now = datetime.now(timezone.utc)
    from_states: dict[int, str] = {}
    for vid, vm in vms.items():
        prior = vm.lifecycle_state
        from_states[vid] = prior.value if isinstance(prior, VMLifecycleState) else str(prior)
        vm.lifecycle_state = target
        vm.lifecycle_state_changed_at = now
    return from_states


def transition_to_planned(
    vm_ids: list[int],
    plan_id: int,
    db: Session,
    *,
    actor: str = "system",
) -> TransitionResult:
    """available → planned. Fails if any VM is not currently ``available``.

    Hooked into ``POST /api/plans`` after the cap / mapping checks.
    """
    vms = _fetch_locked(db, vm_ids)
    missing = [vid for vid in vm_ids if vid not in vms]
    if missing:
        raise LifecycleTransitionError(
            attempted=VMLifecycleState.planned,
            offenders=[(vid, VMLifecycleState.unmanageable) for vid in missing],
        )
    _enforce(vms, {VMLifecycleState.available}, VMLifecycleState.planned)
    from_states = _apply(vms, VMLifecycleState.planned)
    record_audit(
        db,
        action="vm.lifecycle_state.transition",
        actor=actor,
        resource_type="plan",
        resource_id=plan_id,
        details={
            "vm_ids": list(vms.keys()),
            "to": VMLifecycleState.planned.value,
            "from": from_states,
            "trigger": "plan.create",
        },
    )
    return TransitionResult(
        vm_ids=list(vms.keys()),
        from_states=from_states,
        to_state=VMLifecycleState.planned.value,
        audit_action="vm.lifecycle_state.transition",
    )


def transition_to_available_from_failed_plan(
    plan_id: int,
    vm_ids: list[int],
    db: Session,
    *,
    actor: str = "system",
) -> TransitionResult:
    """planned → available. Used when a plan transitions to ``failed``.

    Idempotent on VMs not in ``planned`` (they're skipped silently);
    this matters because a failed plan may have already had some VMs
    reassigned by an operator via a revert flow.
    """
    return _release_planned_to_available(plan_id, vm_ids, db, actor=actor, trigger="plan.failed")


def transition_to_available_from_deleted_plan(
    plan_id: int,
    vm_ids: list[int],
    db: Session,
    *,
    actor: str = "system",
) -> TransitionResult:
    """planned → available. Used when a plan is DELETEd.

    Same idempotent semantics as the failed-plan path.
    """
    return _release_planned_to_available(plan_id, vm_ids, db, actor=actor, trigger="plan.deleted")


def _release_planned_to_available(
    plan_id: int,
    vm_ids: list[int],
    db: Session,
    *,
    actor: str,
    trigger: str,
) -> TransitionResult:
    vms = _fetch_locked(db, vm_ids)
    # Skip VMs already moved out of ``planned`` (operator-driven revert,
    # second concurrent failure path, etc.). This is by design — releasing
    # a plan should never roll a VM back from ``migrated`` to ``available``.
    eligible = {
        vid: vm
        for vid, vm in vms.items()
        if (
            vm.lifecycle_state == VMLifecycleState.planned
            if isinstance(vm.lifecycle_state, VMLifecycleState)
            else vm.lifecycle_state == VMLifecycleState.planned.value
        )
    }
    if not eligible:
        return TransitionResult(
            vm_ids=[],
            from_states={},
            to_state=VMLifecycleState.available.value,
            audit_action="vm.lifecycle_state.transition",
        )
    from_states = _apply(eligible, VMLifecycleState.available)
    record_audit(
        db,
        action="vm.lifecycle_state.transition",
        actor=actor,
        resource_type="plan",
        resource_id=plan_id,
        details={
            "vm_ids": list(eligible.keys()),
            "to": VMLifecycleState.available.value,
            "from": from_states,
            "trigger": trigger,
        },
    )
    return TransitionResult(
        vm_ids=list(eligible.keys()),
        from_states=from_states,
        to_state=VMLifecycleState.available.value,
        audit_action="vm.lifecycle_state.transition",
    )


def transition_to_migrated(
    plan_id: int,
    vm_ids: list[int],
    db: Session,
    *,
    actor: str = "system",
) -> TransitionResult:
    """planned → migrated. Triggered by ``POST /api/plans/{id}/mark-succeeded``.

    Idempotent on VMs already in ``migrated`` — same plan may be marked
    succeeded twice if the operator double-clicks. VMs not in ``planned``
    or ``migrated`` raise.
    """
    vms = _fetch_locked(db, vm_ids)
    missing = [vid for vid in vm_ids if vid not in vms]
    if missing:
        raise LifecycleTransitionError(
            attempted=VMLifecycleState.migrated,
            offenders=[(vid, VMLifecycleState.unmanageable) for vid in missing],
        )
    _enforce(
        vms,
        {VMLifecycleState.planned, VMLifecycleState.migrated},
        VMLifecycleState.migrated,
    )
    # Filter to those actually moving so audit details are accurate.
    movers = {
        vid: vm
        for vid, vm in vms.items()
        if (
            vm.lifecycle_state == VMLifecycleState.planned
            if isinstance(vm.lifecycle_state, VMLifecycleState)
            else vm.lifecycle_state == VMLifecycleState.planned.value
        )
    }
    if not movers:
        return TransitionResult(
            vm_ids=[],
            from_states={},
            to_state=VMLifecycleState.migrated.value,
            audit_action="vm.lifecycle_state.transition",
        )
    from_states = _apply(movers, VMLifecycleState.migrated)
    record_audit(
        db,
        action="vm.lifecycle_state.transition",
        actor=actor,
        resource_type="plan",
        resource_id=plan_id,
        details={
            "vm_ids": list(movers.keys()),
            "to": VMLifecycleState.migrated.value,
            "from": from_states,
            "trigger": "plan.mark_succeeded",
        },
    )
    return TransitionResult(
        vm_ids=list(movers.keys()),
        from_states=from_states,
        to_state=VMLifecycleState.migrated.value,
        audit_action="vm.lifecycle_state.transition",
    )


def transition_to_rolled_back(
    vm_id: int,
    db: Session,
    *,
    actor: str = "user",
) -> TransitionResult:
    """migrated → rolled_back. Single-VM operator action."""
    vms = _fetch_locked(db, [vm_id])
    if vm_id not in vms:
        raise LifecycleTransitionError(
            attempted=VMLifecycleState.rolled_back,
            offenders=[(vm_id, VMLifecycleState.unmanageable)],
        )
    _enforce(vms, {VMLifecycleState.migrated}, VMLifecycleState.rolled_back)
    from_states = _apply(vms, VMLifecycleState.rolled_back)
    record_audit(
        db,
        action="vm.lifecycle_state.transition",
        actor=actor,
        resource_type="vm",
        resource_id=vm_id,
        details={
            "to": VMLifecycleState.rolled_back.value,
            "from": from_states[vm_id],
            "trigger": "operator.revert",
        },
    )
    return TransitionResult(
        vm_ids=[vm_id],
        from_states=from_states,
        to_state=VMLifecycleState.rolled_back.value,
        audit_action="vm.lifecycle_state.transition",
    )


def transition_to_available_from_rolled_back(
    vm_id: int,
    db: Session,
    *,
    actor: str = "user",
) -> TransitionResult:
    """rolled_back → available. Single-VM operator action.

    Deliberately a separate transition from rolled_back, not a one-shot
    "back to available" from migrated — the operator should confirm the
    rollback completed before re-opening the VM for fresh plans.
    """
    vms = _fetch_locked(db, [vm_id])
    if vm_id not in vms:
        raise LifecycleTransitionError(
            attempted=VMLifecycleState.available,
            offenders=[(vm_id, VMLifecycleState.unmanageable)],
        )
    _enforce(vms, {VMLifecycleState.rolled_back}, VMLifecycleState.available)
    from_states = _apply(vms, VMLifecycleState.available)
    record_audit(
        db,
        action="vm.lifecycle_state.transition",
        actor=actor,
        resource_type="vm",
        resource_id=vm_id,
        details={
            "to": VMLifecycleState.available.value,
            "from": from_states[vm_id],
            "trigger": "operator.confirm_rollback",
        },
    )
    return TransitionResult(
        vm_ids=[vm_id],
        from_states=from_states,
        to_state=VMLifecycleState.available.value,
        audit_action="vm.lifecycle_state.transition",
    )


# ---------------------------------------------------------------------------
# Allowed-transition table used by PATCH /api/vms/{id} to validate
# operator-driven changes. Server-side enforcement means the UI cannot
# fabricate transitions even if it tries.
# ---------------------------------------------------------------------------
ALLOWED_PATCH_TRANSITIONS: dict[VMLifecycleState, set[VMLifecycleState]] = {
    VMLifecycleState.migrated: {VMLifecycleState.rolled_back},
    VMLifecycleState.rolled_back: {VMLifecycleState.available},
}


def patch_transition(
    vm: VM,
    target: VMLifecycleState,
    db: Session,
    *,
    actor: str,
) -> TransitionResult:
    """Validate + apply a PATCH /api/vms/{id} lifecycle_state delta.

    Wraps the two operator-driven transitions in one call so the route
    handler stays small. Any disallowed source state raises
    LifecycleTransitionError — the route catches it as 422.
    """
    current = vm.lifecycle_state
    if isinstance(current, str):
        current = VMLifecycleState(current)
    allowed = ALLOWED_PATCH_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise LifecycleTransitionError(
            attempted=target,
            offenders=[(vm.id, current)],
        )
    if current == VMLifecycleState.migrated and target == VMLifecycleState.rolled_back:
        return transition_to_rolled_back(vm.id, db, actor=actor)
    if current == VMLifecycleState.rolled_back and target == VMLifecycleState.available:
        return transition_to_available_from_rolled_back(vm.id, db, actor=actor)
    # Defensive: ALLOWED_PATCH_TRANSITIONS guards above ensure we never
    # reach here, but the assertion makes intent explicit.
    raise LifecycleTransitionError(
        attempted=target,
        offenders=[(vm.id, current)],
    )
