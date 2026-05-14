"""Bulk validation orchestrator.

Mirrors :mod:`app.core.bulk_capture` but for the validation
pipeline — drives a fleet of VMs through SSH-collect → diff → tier
classify → (cached or LLM) verdict → persist. The tier classifier
in ``app.core.validation_tiers`` is the key efficiency lever:
60-80%+ of VMs in a healthy migration land in Tier 1 or Tier 2 and
never hit the LLM.

The orchestrator exposes two surfaces:

  - :func:`run_bulk_validation` — sync entry point used by the
    BackgroundTask wrapper. Walks every VM serially through the
    full pipeline.
  - :func:`preview_tier_distribution` — read-only; runs the
    SSH-collect + diff step on every VM and reports how many would
    land in each tier WITHOUT calling the LLM. The UI calls this
    before submission so the operator sees an LLM-call estimate.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from sqlalchemy.orm import Session

from app.core import db as _db_module
from app.core.audit import record_audit
from app.core.validation import (
    ValidationError,
    run_validation,
)
from app.models.vm import VM

logger = logging.getLogger(__name__)


BulkValidationStatus = Literal["running", "completed", "failed"]


@dataclass
class VMValidationResult:
    vm_id: int
    vm_name: str
    status: Literal["queued", "running", "completed", "failed"] = "queued"
    verdict: Optional[str] = None  # "pass" | "warn" | "fail" once known
    tier: Optional[str] = None  # "tier1" | "tier2" | "tier3"
    cached: bool = False
    needs_manual_review: bool = False
    error: Optional[str] = None
    elapsed_seconds: Optional[int] = None


@dataclass
class BulkValidationTask:
    task_id: str
    status: BulkValidationStatus = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: Optional[datetime] = None
    total: int = 0
    completed: int = 0
    failed: int = 0
    current_vm: Optional[str] = None
    tier_distribution: dict[str, int] = field(
        default_factory=lambda: {"tier1": 0, "tier2": 0, "tier3": 0}
    )
    llm_calls: int = 0
    cache_hits: int = 0
    per_vm: dict[int, VMValidationResult] = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        for key in ("started_at", "completed_at"):
            value = d.get(key)
            if isinstance(value, datetime):
                d[key] = value.isoformat()
        d["per_vm"] = [asdict(r) for r in self.per_vm.values()]
        return d


class BulkValidationTaskStore:
    def __init__(self) -> None:
        self._tasks: dict[str, BulkValidationTask] = {}
        self._lock = threading.RLock()

    def create(self, *, total: int) -> BulkValidationTask:
        task = BulkValidationTask(
            task_id=str(uuid.uuid4()),
            total=total,
        )
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id: str) -> Optional[BulkValidationTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def queue_vm(self, task_id: str, vm_id: int, vm_name: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.per_vm[vm_id] = VMValidationResult(vm_id=vm_id, vm_name=vm_name)

    def start_vm(self, task_id: str, vm_id: int, vm_name: str) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            entry = t.per_vm.get(vm_id) or VMValidationResult(vm_id=vm_id, vm_name=vm_name)
            entry.status = "running"
            t.per_vm[vm_id] = entry
            t.current_vm = vm_name

    def complete_vm(
        self,
        task_id: str,
        vm_id: int,
        *,
        verdict: str,
        tier: str,
        cached: bool,
        needs_manual_review: bool,
        elapsed_seconds: int,
    ) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            entry = t.per_vm.get(vm_id)
            if entry is None:
                return
            entry.status = "completed"
            entry.verdict = verdict
            entry.tier = tier
            entry.cached = cached
            entry.needs_manual_review = needs_manual_review
            entry.elapsed_seconds = elapsed_seconds
            t.completed += 1
            if tier in t.tier_distribution:
                t.tier_distribution[tier] += 1
            if tier == "tier3":
                if cached:
                    t.cache_hits += 1
                else:
                    t.llm_calls += 1

    def fail_vm(self, task_id: str, vm_id: int, *, error: str, elapsed_seconds: int) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            entry = t.per_vm.get(vm_id)
            if entry is None:
                return
            entry.status = "failed"
            entry.error = error
            entry.elapsed_seconds = elapsed_seconds
            t.failed += 1

    def finalize(self, task_id: str, *, status: BulkValidationStatus) -> None:
        with self._lock:
            t = self._tasks.get(task_id)
            if t is None:
                return
            t.status = status
            t.completed_at = datetime.now(timezone.utc)
            t.current_vm = None


task_store = BulkValidationTaskStore()


# ---------------------------------------------------------------------------
# Sync entry point — driven by FastAPI BackgroundTask
# ---------------------------------------------------------------------------
def run_bulk_validation(
    task_id: str,
    *,
    vm_ids: list[int],
    actor: str,
    use_cache: bool = True,
) -> None:
    """Drive a bulk validation run to completion.

    Single-threaded by design — every VM hits SSH + the LLM (in the
    Tier 3 case), and parallel LLM calls already get rate-limited by
    the backend's ``max_concurrent_calls``. Going parallel here would
    just queue at the backend without speeding anything up.
    """
    db = _db_module.SessionLocal()
    try:
        vms = list(db.query(VM).filter(VM.id.in_(vm_ids)).all())
        for vm in vms:
            task_store.queue_vm(task_id, vm.id, vm.name)

        for vm in vms:
            task_store.start_vm(task_id, vm.id, vm.name)
            started = time.monotonic()
            try:
                row = run_validation(db, vm, actor=actor, use_cache=use_cache)
                # Verdict + tier come from the most recent audit row;
                # easier to pull them from the just-persisted row +
                # its diff metadata.
                tier = (row.diff or {}).get("_tier") or "tier3"
                cached = bool((row.diff or {}).get("_cached"))
                needs_manual = bool((row.diff or {}).get("_needs_manual_review"))
                # Fall back to the tier/cached info stamped on the
                # latest audit entry if the diff metadata isn't there.
                if tier == "tier3" and not cached:
                    # We need a way to tell tier3-fresh vs tier3-cached.
                    # The audit row carries this; query it.
                    tier, cached, needs_manual = _read_last_validation_meta(db, vm.id)
                task_store.complete_vm(
                    task_id,
                    vm.id,
                    verdict=row.status.value,
                    tier=tier or "tier3",
                    cached=cached,
                    needs_manual_review=needs_manual,
                    elapsed_seconds=int(time.monotonic() - started),
                )
            except ValidationError as e:
                logger.warning("bulk validation failed for vm %s: %s", vm.name, e)
                task_store.fail_vm(
                    task_id,
                    vm.id,
                    error=str(e),
                    elapsed_seconds=int(time.monotonic() - started),
                )
            except Exception as e:  # noqa: BLE001 — defensive
                logger.exception("unexpected error in bulk validation: %s", e)
                task_store.fail_vm(
                    task_id,
                    vm.id,
                    error=f"{type(e).__name__}: {e}",
                    elapsed_seconds=int(time.monotonic() - started),
                )

        task_store.finalize(task_id, status="completed")
        task = task_store.get(task_id)
        if task is not None:
            record_audit(
                db,
                action="validation.bulk_completed",
                actor=actor,
                resource_type="bulk_validation",
                resource_id=None,
                details={
                    "task_id": task_id,
                    "total": task.total,
                    "completed": task.completed,
                    "failed": task.failed,
                    "tier_distribution": dict(task.tier_distribution),
                    "llm_calls": task.llm_calls,
                    "cache_hits": task.cache_hits,
                },
            )
            db.commit()
    finally:
        db.close()


def _read_last_validation_meta(db: Session, vm_id: int) -> tuple[str, bool, bool]:
    """Pull tier + cached + needs_manual_review off the most recent
    validation.completed audit row for this VM.

    The tier classifier writes these into the audit row's ``details``;
    reading them here keeps the bulk task store accurate without
    forcing every caller of ``run_validation`` to thread them back
    through the return type.
    """
    from sqlalchemy import select

    from app.models.audit import AuditLog

    row = db.scalars(
        select(AuditLog)
        .where(AuditLog.action == "validation.completed")
        .where(AuditLog.resource_type == "validation")
        .order_by(AuditLog.id.desc())
        .limit(50)
    ).all()
    for entry in row:
        details = entry.details or {}
        if details.get("vm_id") == vm_id:
            return (
                details.get("tier") or "tier3",
                bool(details.get("cached")),
                bool(details.get("needs_manual_review")),
            )
    return ("tier3", False, False)


# ---------------------------------------------------------------------------
# Preview — runs SSH-collect + diff + classifier but never the LLM.
# ---------------------------------------------------------------------------
def preview_tier_distribution(db: Session, *, vm_ids: list[int], actor: str) -> dict:
    """Inspect every VM's diff and report estimated tier counts.

    SSH-collects current state for each VM (cheap relative to the
    LLM call), runs the structured diff, runs the tier classifier.
    Returns:

        {
          "total": 47,
          "tier1": 25, "tier2": 10, "tier3": 12,
          "estimated_llm_calls": 12,
          "cache_estimate": "unknown" | "<n cache hits expected>",
          "errors": ["vm-foo: no baseline captured", ...]
        }
    """
    from app.core.llm.client import compute_diff
    from app.core.validation import _baseline_profile_dict, _collect_current_state
    from app.core.validation_cache import lookup as cache_lookup
    from app.core.validation_tiers import classify

    counts = Counter()
    errors: list[str] = []
    likely_cache_hits = 0

    vms = list(db.query(VM).filter(VM.id.in_(vm_ids)).all())
    for vm in vms:
        baseline = _baseline_profile_dict(db, vm)
        if baseline is None:
            errors.append(f"{vm.name}: no baseline captured")
            continue
        try:
            current = _collect_current_state(db, vm, actor=actor)
        except Exception as e:  # noqa: BLE001 — preview is best-effort
            errors.append(f"{vm.name}: collection failed ({e})")
            continue
        diff = compute_diff(baseline, current)
        result = classify(diff, environment=vm.environment)
        counts[result.tier] += 1
        if result.tier == "tier3":
            os_family = ((baseline.get("meta") or {}).get("os_profile") or {}).get("distro_family")
            if cache_lookup(db, diff=diff, os_family=os_family) is not None:
                likely_cache_hits += 1

    return {
        "total": len(vms),
        "tier1": int(counts["tier1"]),
        "tier2": int(counts["tier2"]),
        "tier3": int(counts["tier3"]),
        "estimated_llm_calls": int(counts["tier3"]) - likely_cache_hits,
        "cache_estimate": likely_cache_hits,
        "errors": errors,
    }
