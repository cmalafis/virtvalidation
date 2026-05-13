"""Tests for the VM plan-membership lifecycle service.

Pins every documented transition + every rejected transition. The
state machine is server-enforced; tests here are the regression net
that catches a UI starting to send disallowed PATCH payloads.
"""

from __future__ import annotations

import pytest

from app.core.vm_lifecycle import (
    LifecycleTransitionError,
    transition_to_available_from_deleted_plan,
    transition_to_available_from_failed_plan,
    transition_to_available_from_rolled_back,
    transition_to_migrated,
    transition_to_planned,
    transition_to_rolled_back,
)
from app.models.audit import AuditLog
from app.models.vm import VM, VMLifecycleState


def _make_vm(db, name: str = "test-vm", state: VMLifecycleState = VMLifecycleState.available) -> VM:
    vm = VM(
        name=name,
        source_hostname=f"{name}.example",
        lifecycle_state=state,
    )
    db.add(vm)
    db.flush()
    return vm


def _audit_count(db, *, action: str) -> int:
    from sqlalchemy import func, select

    return db.scalar(select(func.count(AuditLog.id)).where(AuditLog.action == action)) or 0


# ---------------------------------------------------------------------------
# Happy-path transitions
# ---------------------------------------------------------------------------
class TestPlannedTransition:
    def test_available_to_planned(self, db_session):
        vm = _make_vm(db_session, "vm-1")
        result = transition_to_planned([vm.id], plan_id=42, db=db_session)
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.planned
        assert result.vm_ids == [vm.id]
        assert result.from_states == {vm.id: VMLifecycleState.available.value}

    def test_audit_row_written(self, db_session):
        vm = _make_vm(db_session)
        before = _audit_count(db_session, action="vm.lifecycle_state.transition")
        transition_to_planned([vm.id], plan_id=42, db=db_session)
        db_session.commit()
        assert _audit_count(db_session, action="vm.lifecycle_state.transition") == before + 1

    def test_rejects_already_planned(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.planned)
        with pytest.raises(LifecycleTransitionError) as exc:
            transition_to_planned([vm.id], plan_id=42, db=db_session)
        assert exc.value.attempted == VMLifecycleState.planned

    def test_rejects_unknown_vm(self, db_session):
        with pytest.raises(LifecycleTransitionError):
            transition_to_planned([99999], plan_id=42, db=db_session)

    def test_partial_failure_reports_all_offenders(self, db_session):
        ok = _make_vm(db_session, "vm-ok")
        bad = _make_vm(db_session, "vm-bad", state=VMLifecycleState.planned)
        with pytest.raises(LifecycleTransitionError) as exc:
            transition_to_planned([ok.id, bad.id], plan_id=42, db=db_session)
        # Only one offender — the planned VM
        assert exc.value.offenders == [(bad.id, VMLifecycleState.planned)]


class TestMigrateTransition:
    def test_planned_to_migrated(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.planned)
        transition_to_migrated(plan_id=1, vm_ids=[vm.id], db=db_session)
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.migrated

    def test_idempotent_on_already_migrated(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.migrated)
        # Should not raise — VM in migrated is in the allowed-from set.
        result = transition_to_migrated(plan_id=1, vm_ids=[vm.id], db=db_session)
        db_session.commit()
        # No new transition recorded since VM was already migrated
        assert result.vm_ids == []

    def test_rejects_available(self, db_session):
        vm = _make_vm(db_session)
        with pytest.raises(LifecycleTransitionError):
            transition_to_migrated(plan_id=1, vm_ids=[vm.id], db=db_session)


class TestReleaseFromFailedPlan:
    def test_planned_to_available(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.planned)
        transition_to_available_from_failed_plan(plan_id=1, vm_ids=[vm.id], db=db_session)
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.available

    def test_skips_already_migrated_vms(self, db_session):
        # A VM operator-marked as migrated should NOT be rolled back
        # when its plan failed — release is idempotent.
        vm_migrated = _make_vm(db_session, "m", state=VMLifecycleState.migrated)
        vm_planned = _make_vm(db_session, "p", state=VMLifecycleState.planned)
        result = transition_to_available_from_failed_plan(
            plan_id=1, vm_ids=[vm_migrated.id, vm_planned.id], db=db_session
        )
        db_session.commit()
        db_session.refresh(vm_migrated)
        db_session.refresh(vm_planned)
        assert vm_migrated.lifecycle_state == VMLifecycleState.migrated
        assert vm_planned.lifecycle_state == VMLifecycleState.available
        # Only the planned VM moved.
        assert result.vm_ids == [vm_planned.id]


class TestReleaseFromDeletedPlan:
    def test_planned_to_available(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.planned)
        transition_to_available_from_deleted_plan(plan_id=1, vm_ids=[vm.id], db=db_session)
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.available


class TestRollbackPath:
    def test_migrated_to_rolled_back(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.migrated)
        transition_to_rolled_back(vm.id, db_session, actor="op")
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.rolled_back

    def test_rejects_from_available(self, db_session):
        vm = _make_vm(db_session)
        with pytest.raises(LifecycleTransitionError):
            transition_to_rolled_back(vm.id, db_session)

    def test_rolled_back_to_available(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.rolled_back)
        transition_to_available_from_rolled_back(vm.id, db_session, actor="op")
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.available

    def test_rolled_back_rejects_from_migrated(self, db_session):
        vm = _make_vm(db_session, state=VMLifecycleState.migrated)
        with pytest.raises(LifecycleTransitionError):
            transition_to_available_from_rolled_back(vm.id, db_session)


class TestPatchTransition:
    """The PATCH /api/vms/{id} contract: only specific deltas accepted."""

    def test_migrated_to_rolled_back_allowed(self, db_session):
        from app.core.vm_lifecycle import patch_transition

        vm = _make_vm(db_session, state=VMLifecycleState.migrated)
        patch_transition(vm, VMLifecycleState.rolled_back, db_session, actor="op")
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.rolled_back

    def test_rolled_back_to_available_allowed(self, db_session):
        from app.core.vm_lifecycle import patch_transition

        vm = _make_vm(db_session, state=VMLifecycleState.rolled_back)
        patch_transition(vm, VMLifecycleState.available, db_session, actor="op")
        db_session.commit()
        db_session.refresh(vm)
        assert vm.lifecycle_state == VMLifecycleState.available

    def test_planned_to_available_not_allowed_via_patch(self, db_session):
        from app.core.vm_lifecycle import patch_transition

        # PATCH cannot bypass the plan-driven release path. Operators
        # must delete the plan or wait for it to fail.
        vm = _make_vm(db_session, state=VMLifecycleState.planned)
        with pytest.raises(LifecycleTransitionError):
            patch_transition(vm, VMLifecycleState.available, db_session, actor="op")

    def test_available_to_planned_not_allowed_via_patch(self, db_session):
        from app.core.vm_lifecycle import patch_transition

        # PATCH cannot bypass plan creation.
        vm = _make_vm(db_session)
        with pytest.raises(LifecycleTransitionError):
            patch_transition(vm, VMLifecycleState.planned, db_session, actor="op")

    def test_migrated_to_available_skipping_rolled_back_not_allowed(self, db_session):
        from app.core.vm_lifecycle import patch_transition

        # Deliberate two-step: operator must move through rolled_back.
        vm = _make_vm(db_session, state=VMLifecycleState.migrated)
        with pytest.raises(LifecycleTransitionError):
            patch_transition(vm, VMLifecycleState.available, db_session, actor="op")
