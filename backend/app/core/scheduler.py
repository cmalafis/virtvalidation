"""
Scheduled baseline collection.

Runs SSH collection against every enrolled VM on the configured cadence
(twice/once daily or hourly) and persists each result via the shared
``collect_and_store`` helper so the scheduled and manual paths stay in
lockstep.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select

from app.core import db as _db_module  # late-binding for test rebind compat
from app.core.capture import CaptureError, collect_and_store
from app.core.config import settings
from app.core.ssh import SSHCollector
from app.models.settings import AppSettings, SchedulePreset
from app.models.vm import VM

logger = logging.getLogger(__name__)

_JOB_ID = "baseline-collection"

_scheduler: Optional[BackgroundScheduler] = None


def _trigger_for(preset: SchedulePreset) -> CronTrigger:
    """Map a high-level preset to an APScheduler CronTrigger (UTC)."""
    if preset == SchedulePreset.once_daily:
        return CronTrigger(hour=6, minute=0)
    if preset == SchedulePreset.hourly:
        return CronTrigger(minute=0)
    return CronTrigger(hour="6,18", minute=0)  # twice_daily (default)


def _load_preset() -> SchedulePreset:
    db = _db_module.SessionLocal()
    try:
        existing = db.get(AppSettings, 1)
        return existing.schedule_preset if existing else SchedulePreset.twice_daily
    finally:
        db.close()


def collect_baselines_for_all_vms() -> None:
    """Iterate every enrolled VM and capture a fresh baseline."""
    db = _db_module.SessionLocal()
    try:
        # Re-read the host-key policy each scheduled run so flipping the
        # Settings toggle takes effect on the next pass without a restart.
        from app.core.capture import _resolve_host_key_policy  # avoid cycle

        collector = SSHCollector(
            key_path=settings.ssh_key_path,
            host_key_policy=_resolve_host_key_policy(db),
        )
        vms = list(db.scalars(select(VM)).all())
        logger.info("scheduled baseline collection starting for %d VMs", len(vms))
        for vm in vms:
            try:
                snapshot = collect_and_store(db, vm, actor="scheduler", collector=collector)
            except CaptureError as e:
                logger.error("baseline collection failed for %s: %s", vm.name, e)
                continue
            logger.info(
                "baseline #%d stored for VM %s",
                snapshot.snapshot_number,
                vm.name,
            )
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    """Start the background scheduler with the configured baseline cadence."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    preset = _load_preset()
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        collect_baselines_for_all_vms,
        trigger=_trigger_for(preset),
        id=_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("baseline scheduler started with preset=%s", preset.value)
    return scheduler


def reschedule_baseline_job(preset: SchedulePreset) -> None:
    """Apply a new schedule preset to the running baseline job."""
    if _scheduler is None:
        return
    _scheduler.reschedule_job(_JOB_ID, trigger=_trigger_for(preset))
    logger.info("baseline scheduler rescheduled to preset=%s", preset.value)


def next_run_time() -> Optional[datetime]:
    """Return the scheduler's next planned run, or None if not running.

    The Settings page surfaces this as "Next collection: in 3h 42m" — the
    UI does the relative-time formatting; we just hand back the absolute
    UTC datetime so the answer doesn't drift across the request lifecycle.
    """
    if _scheduler is None:
        return None
    job = _scheduler.get_job(_JOB_ID)
    if job is None:
        return None
    return job.next_run_time


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("baseline scheduler stopped")
