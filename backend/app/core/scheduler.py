"""
Scheduled baseline collection.

Runs SSH collection against every enrolled VM twice daily (06:00 and 18:00
local time) and persists each result as a BaselineSnapshot.
"""

from __future__ import annotations

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import func, select

from app.core.audit import record_audit
from app.core.config import settings
from app.core.db import SessionLocal
from app.core.ssh import SSHCollectionError, SSHCollector
from app.models.settings import AppSettings, SchedulePreset
from app.models.vm import VM, BaselineSnapshot, VMStatus

logger = logging.getLogger(__name__)

_SCHEDULED_SSH_USER = "virtvalidate"
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
    db = SessionLocal()
    try:
        existing = db.get(AppSettings, 1)
        return existing.schedule_preset if existing else SchedulePreset.twice_daily
    finally:
        db.close()


def collect_baselines_for_all_vms() -> None:
    """Iterate every enrolled VM and store a fresh baseline snapshot."""
    collector = SSHCollector(key_path=settings.ssh_key_path)
    db = SessionLocal()
    try:
        vms = list(db.scalars(select(VM)).all())
        logger.info("scheduled baseline collection starting for %d VMs", len(vms))
        for vm in vms:
            host = vm.ip_address or vm.source_hostname
            if not host:
                logger.warning("skipping VM %s: no host/ip", vm.name)
                continue
            ssh_user = vm.ssh_user or _SCHEDULED_SSH_USER
            try:
                state = collector.collect(host=host, username=ssh_user)
            except SSHCollectionError as e:
                logger.error("baseline collection failed for %s: %s", vm.name, e)
                continue

            next_number = (
                db.scalar(
                    select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0)).where(
                        BaselineSnapshot.vm_id == vm.id
                    )
                )
                or 0
            ) + 1
            snapshot = BaselineSnapshot(
                vm_id=vm.id,
                snapshot_number=next_number,
                ssh_user=ssh_user,
                raw_data=state,
            )
            db.add(snapshot)
            if vm.status == VMStatus.discovered:
                vm.status = VMStatus.baseline_captured
            db.commit()
            db.refresh(snapshot)
            record_audit(
                db,
                action="baseline.collected",
                actor="scheduler",
                resource_type="baseline",
                resource_id=snapshot.id,
                details={
                    "vm_id": vm.id,
                    "vm_name": vm.name,
                    "snapshot_number": next_number,
                    "ssh_user": ssh_user,
                },
            )
            db.commit()
            logger.info("baseline #%d stored for VM %s", next_number, vm.name)
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


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("baseline scheduler stopped")
