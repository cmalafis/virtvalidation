"""Background-task orchestration for wave-scoped baseline + validation runs.

This module is the glue between the API layer (``app.api.waves``) and the
collection engine + orchestrator. The API handler creates a BaselineRun /
ValidationRun row and the per-VM children, returns 202 with the run id,
and registers one of these functions as a FastAPI BackgroundTask. The
task opens its own DB session (the request's session is closed by the
time the BG task fires) and drives the orchestrator to completion.

Per-VM progress is committed incrementally so the UI's polling sees the
counters tick. The run row never leaves a non-terminal status — even on
unhandled failure we mark it ``failed`` with an error message so the
operator gets a clear surface.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.collection.diff import diff_collection
from app.core.collection.engine import CollectionEngine, CollectionResult, VMTarget
from app.core.collection.orchestrator import run_collection_batch
from app.core.config import settings as app_config
from app.models.baseline_run import (
    Baseline,
    BaselineRun,
    BaselineRunStatus,
    VMCollectionStatus,
)
from app.models.plan import MigrationPlan
from app.models.validation_run import (
    ValidationRun,
    ValidationRunStatus,
    VMValidation,
    VMValidationVerdict,
)
from app.models.vm import VM
from app.services import ssh_key_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def resolve_wave_vm_ids(plan: MigrationPlan, wave_number: int) -> list[int]:
    """Return the VM ids in ``plan.waves[wave_number-1].vm_ids``.

    Raises ``ValueError`` if the wave is out of range or the JSON shape
    doesn't match the expected ``{wave_number, vm_ids: [...]}`` schema.
    """
    waves = plan.waves or []
    for w in waves:
        if not isinstance(w, dict):
            continue
        if w.get("wave_number") == wave_number:
            vm_ids = w.get("vm_ids") or []
            return [int(v) for v in vm_ids]
    raise ValueError(
        f"Plan {plan.id} has no wave numbered {wave_number} " f"(plan has {len(waves)} wave(s))"
    )


def build_targets(db: Session, vm_ids: list[int]) -> list[VMTarget]:
    """Translate VM rows into engine-ready :class:`VMTarget` objects.

    A VM with no resolvable host address still produces a target — the
    engine will categorize it as ``unreachable`` and surface the row
    rather than silently dropping it.
    """
    rows = list(db.scalars(select(VM).where(VM.id.in_(vm_ids))).all())
    by_id = {vm.id: vm for vm in rows}
    targets: list[VMTarget] = []
    for vm_id in vm_ids:
        vm = by_id.get(vm_id)
        if vm is None:
            # VM was deleted between run-creation and BG-task-start. Surface
            # as an unreachable target so the per-VM row gets a meaningful
            # failure category.
            targets.append(VMTarget(vm_id=vm_id, host=""))
            continue
        host = vm.ip_address or vm.target_hostname or vm.source_hostname or ""
        targets.append(
            VMTarget(
                vm_id=vm.id,
                host=host,
                port=vm.ssh_port or 22,
                username=vm.ssh_user or "virtvalidate",
                vcenter_id=vm.source_vcenter_id,
            )
        )
    return targets


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _persist_command_audits(
    db: Session,
    *,
    result: CollectionResult,
    run_type: str,
    run_id: int,
    host: str,
) -> None:
    """Write one CommandAudit row per command the collector ran on this VM.

    Best-effort: a failure to persist the audit trail must not abort the
    run's progress, so callers invoke this inside the on_complete callback
    whose own exceptions are already swallowed by the orchestrator.
    """
    from app.models.command_audit import CommandAudit

    for rec in result.command_log or []:
        db.add(
            CommandAudit(
                vm_id=result.vm_id,
                host=host or "",
                run_type=run_type,
                run_id=run_id,
                command=rec.get("command", ""),
                exit_status=rec.get("exit_status"),
                stdout_byte_count=int(rec.get("stdout_byte_count") or 0),
                stdout_sha256=rec.get("stdout_sha256"),
                stdout_truncated=rec.get("stdout_truncated"),
                duration_ms=int(rec.get("duration_ms") or 0),
                blocked=bool(rec.get("blocked")),
            )
        )


# ---------------------------------------------------------------------------
# Baseline run
# ---------------------------------------------------------------------------
def run_baseline_task(baseline_run_id: int) -> None:
    """BackgroundTask entry point — drives one BaselineRun to completion.

    Opens its own DB session. The session is committed incrementally as
    each VM completes so the UI's polling sees progress in real time.
    """
    db = _db_module.SessionLocal()
    try:
        run = db.get(BaselineRun, baseline_run_id)
        if run is None:
            logger.error("baseline_run.missing id=%s", baseline_run_id)
            return
        plan = db.get(MigrationPlan, run.plan_id)
        if plan is None:
            run.status = BaselineRunStatus.failed
            run.progress_message = f"Plan {run.plan_id} not found"
            run.completed_at = _utcnow()
            db.commit()
            return

        run.status = BaselineRunStatus.running
        run.started_at = _utcnow()
        run.progress_message = "Loading SSH key"
        db.commit()

        try:
            paramiko_key = ssh_key_service.load_paramiko_key(db, run.ssh_key_id)
        except (
            ssh_key_service.SSHKeyNotFoundError,
            ssh_key_service.SSHKeyRetiredError,
            ssh_key_service.PrivateKeyMissingError,
        ) as e:
            run.status = BaselineRunStatus.failed
            run.progress_message = f"SSH key unusable: {e}"
            run.completed_at = _utcnow()
            db.commit()
            return

        # The Baseline rows were already created (status=pending) by the
        # API handler so the operator sees them in the response. Build a
        # vm_id -> Baseline lookup so the callback can update each row.
        baselines = list(
            db.scalars(select(Baseline).where(Baseline.baseline_run_id == run.id)).all()
        )
        baselines_by_vm = {b.vm_id: b for b in baselines}
        vm_ids = [b.vm_id for b in baselines]
        targets = build_targets(db, vm_ids)
        host_by_vm = {t.vm_id: t.host for t in targets}

        engine = CollectionEngine(
            paramiko_key=paramiko_key,
            connect_timeout=app_config.ssh_connect_timeout_seconds,
            command_timeout=app_config.ssh_command_timeout_seconds,
        )

        run.progress_message = f"Collecting from {len(targets)} VMs"
        db.commit()

        def on_complete(result: CollectionResult) -> None:
            row = baselines_by_vm.get(result.vm_id)
            if row is None:
                logger.warning(
                    "baseline_run.unexpected_vm run_id=%s vm_id=%s",
                    run.id,
                    result.vm_id,
                )
                return
            row.collection_started_at = result.started_at
            row.collection_completed_at = result.completed_at
            row.host_key_fingerprint = result.host_key_fingerprint
            row.probe_catalog_version = result.probe_catalog_version
            row.probes_run = list(result.probes_run)
            if result.succeeded:
                row.status = VMCollectionStatus.captured
                row.collected_data = result.collected_data
                # Partial collections still set failure_category for UI display.
                row.failure_category = result.failure_category
                row.failure_detail = result.failure_detail
                run.captured_vms = (run.captured_vms or 0) + 1
            else:
                row.status = VMCollectionStatus.failed
                row.failure_category = result.failure_category
                row.failure_detail = result.failure_detail
                run.failed_vms = (run.failed_vms or 0) + 1
            _persist_command_audits(
                db,
                result=result,
                run_type="baseline",
                run_id=run.id,
                host=host_by_vm.get(result.vm_id, ""),
            )
            run.progress_message = (
                f"{run.captured_vms}/{run.total_vms} captured, " f"{run.failed_vms} failed"
            )
            db.commit()

        try:
            asyncio.run(
                run_collection_batch(
                    targets=targets,
                    engine=engine,
                    max_concurrency=app_config.ssh_max_concurrency,
                    max_concurrency_per_vcenter=app_config.ssh_max_concurrency_per_vcenter,
                    circuit_breaker_threshold=app_config.ssh_circuit_breaker_threshold,
                    on_vm_complete=on_complete,
                )
            )
        except Exception as e:  # noqa: BLE001 — orchestrator promises no raise; belt-and-braces
            logger.exception("baseline_run.orchestrator_failed id=%s", run.id)
            run.status = BaselineRunStatus.failed
            run.progress_message = f"Orchestrator failure: {e}"
            run.completed_at = _utcnow()
            db.commit()
            return

        run.status = BaselineRunStatus.completed
        run.completed_at = _utcnow()
        run.progress_message = f"Completed: {run.captured_vms} captured, {run.failed_vms} failed"
        db.commit()
        logger.info(
            "baseline_run.completed id=%s captured=%s failed=%s",
            run.id,
            run.captured_vms,
            run.failed_vms,
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Validation run
# ---------------------------------------------------------------------------
def _latest_completed_baseline_run(
    db: Session, plan_id: int, wave_number: int
) -> BaselineRun | None:
    """Return the most recent completed BaselineRun for this wave, or None.

    "Most recent" = highest id among completed runs. The 422 check at the
    API layer uses this so an operator can't kick off validation against
    a wave that's never been baselined.
    """
    return db.scalars(
        select(BaselineRun)
        .where(
            BaselineRun.plan_id == plan_id,
            BaselineRun.wave_number == wave_number,
            BaselineRun.status == BaselineRunStatus.completed,
        )
        .order_by(BaselineRun.id.desc())
    ).first()


def _verdict_from_diff(diff: dict) -> VMValidationVerdict:
    overall = (diff or {}).get("overall", "pass")
    if overall == "fail":
        return VMValidationVerdict.failed
    if overall == "warn":
        return VMValidationVerdict.warned
    return VMValidationVerdict.passed


def run_validation_task(validation_run_id: int) -> None:
    """BackgroundTask entry point — drives one ValidationRun to completion."""
    db = _db_module.SessionLocal()
    try:
        run = db.get(ValidationRun, validation_run_id)
        if run is None:
            logger.error("validation_run.missing id=%s", validation_run_id)
            return

        run.status = ValidationRunStatus.running
        run.started_at = _utcnow()
        run.progress_message = "Loading SSH key"
        db.commit()

        try:
            paramiko_key = ssh_key_service.load_paramiko_key(db, run.ssh_key_id)
        except (
            ssh_key_service.SSHKeyNotFoundError,
            ssh_key_service.SSHKeyRetiredError,
            ssh_key_service.PrivateKeyMissingError,
        ) as e:
            run.status = ValidationRunStatus.failed
            run.progress_message = f"SSH key unusable: {e}"
            run.completed_at = _utcnow()
            db.commit()
            return

        baseline_run = _latest_completed_baseline_run(db, run.plan_id, run.wave_number)
        if baseline_run is None:
            run.status = ValidationRunStatus.failed
            run.progress_message = "No completed baseline run for this wave — cannot validate"
            run.completed_at = _utcnow()
            db.commit()
            return

        # Build a vm_id → baseline (captured-only) lookup so the callback can
        # diff each result against the correct baseline. Failed baselines
        # are skipped — their corresponding validation rows will be marked
        # unreachable since there's nothing to compare against.
        baseline_rows = list(
            db.scalars(select(Baseline).where(Baseline.baseline_run_id == baseline_run.id)).all()
        )
        baselines_by_vm: dict[int, Baseline] = {b.vm_id: b for b in baseline_rows}

        # The VMValidation rows were pre-created by the API handler.
        validation_rows = list(
            db.scalars(select(VMValidation).where(VMValidation.validation_run_id == run.id)).all()
        )
        validations_by_vm = {v.vm_id: v for v in validation_rows}

        vm_ids = [v.vm_id for v in validation_rows]
        targets = build_targets(db, vm_ids)
        host_by_vm = {t.vm_id: t.host for t in targets}

        engine = CollectionEngine(
            paramiko_key=paramiko_key,
            connect_timeout=app_config.ssh_connect_timeout_seconds,
            command_timeout=app_config.ssh_command_timeout_seconds,
        )

        run.progress_message = f"Validating {len(targets)} VMs"
        db.commit()

        def on_complete(result: CollectionResult) -> None:
            row = validations_by_vm.get(result.vm_id)
            if row is None:
                logger.warning(
                    "validation_run.unexpected_vm run_id=%s vm_id=%s",
                    run.id,
                    result.vm_id,
                )
                return

            baseline = baselines_by_vm.get(result.vm_id)
            row.baseline_id = baseline.id if baseline is not None else None

            if not result.succeeded:
                row.verdict = VMValidationVerdict.unreachable
                row.collected_data = result.collected_data
                row.failure_category = result.failure_category
                row.failure_detail = result.failure_detail
                run.unreachable_vms = (run.unreachable_vms or 0) + 1
            else:
                row.collected_data = result.collected_data
                if baseline is None or baseline.collected_data is None:
                    # Successful collection but nothing to diff against —
                    # treat as unreachable for the verdict, since the user
                    # can't compare. Surface the cause.
                    row.verdict = VMValidationVerdict.unreachable
                    row.failure_category = "partial"
                    row.failure_detail = "No baseline available to compare against"
                    run.unreachable_vms = (run.unreachable_vms or 0) + 1
                else:
                    diff = diff_collection(baseline.collected_data, result.collected_data)
                    row.diff_result = diff
                    row.verdict = _verdict_from_diff(diff)
                    row.failure_category = result.failure_category
                    row.failure_detail = result.failure_detail
                    if row.verdict == VMValidationVerdict.passed:
                        run.passed_vms = (run.passed_vms or 0) + 1
                    elif row.verdict == VMValidationVerdict.warned:
                        run.warned_vms = (run.warned_vms or 0) + 1
                    else:
                        run.failed_vms = (run.failed_vms or 0) + 1

                # Host-key delta is informational; never escalates the
                # verdict (handled by diff_collection's kernel_os/info path).
                if (
                    baseline is not None
                    and baseline.host_key_fingerprint
                    and result.host_key_fingerprint
                    and baseline.host_key_fingerprint != result.host_key_fingerprint
                ):
                    row.host_key_changed = True

            _persist_command_audits(
                db,
                result=result,
                run_type="validation",
                run_id=run.id,
                host=host_by_vm.get(result.vm_id, ""),
            )
            row.validated_at = _utcnow()
            total_done = run.passed_vms + run.warned_vms + run.failed_vms + run.unreachable_vms
            run.progress_message = (
                f"{total_done}/{run.total_vms} validated "
                f"(p={run.passed_vms} w={run.warned_vms} "
                f"f={run.failed_vms} u={run.unreachable_vms})"
            )
            db.commit()

        try:
            asyncio.run(
                run_collection_batch(
                    targets=targets,
                    engine=engine,
                    max_concurrency=app_config.ssh_max_concurrency,
                    max_concurrency_per_vcenter=app_config.ssh_max_concurrency_per_vcenter,
                    circuit_breaker_threshold=app_config.ssh_circuit_breaker_threshold,
                    on_vm_complete=on_complete,
                )
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("validation_run.orchestrator_failed id=%s", run.id)
            run.status = ValidationRunStatus.failed
            run.progress_message = f"Orchestrator failure: {e}"
            run.completed_at = _utcnow()
            db.commit()
            return

        run.status = ValidationRunStatus.completed
        run.completed_at = _utcnow()
        run.progress_message = (
            f"Completed: p={run.passed_vms} w={run.warned_vms} "
            f"f={run.failed_vms} u={run.unreachable_vms}"
        )
        db.commit()
    finally:
        db.close()
