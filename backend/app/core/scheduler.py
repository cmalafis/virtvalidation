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

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.ssh import SSHCollectionError, SSHCollector
from app.models.vm import VM, BaselineSnapshot, VMStatus

logger = logging.getLogger(__name__)

_SCHEDULED_SSH_USER = "virtvalidate"
_JOB_ID = "baseline-collection"

_scheduler: Optional[BackgroundScheduler] = None


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
            try:
                state = collector.collect(host=host, username=_SCHEDULED_SSH_USER)
            except SSHCollectionError as e:
                logger.error("baseline collection failed for %s: %s", vm.name, e)
                continue

            next_number = (
                db.scalar(
                    select(func.coalesce(func.max(BaselineSnapshot.snapshot_number), 0))
                    .where(BaselineSnapshot.vm_id == vm.id)
                )
                + 1
            )
            snapshot = BaselineSnapshot(
                vm_id=vm.id,
                snapshot_number=next_number,
                ssh_user=_SCHEDULED_SSH_USER,
                raw_data=state,
            )
            db.add(snapshot)
            if vm.status == VMStatus.discovered:
                vm.status = VMStatus.baseline_captured
            db.commit()
            logger.info(
                "baseline #%d stored for VM %s", next_number, vm.name
            )
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    """Start the background scheduler with the twice-daily baseline job."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        collect_baselines_for_all_vms,
        trigger=CronTrigger(hour="6,18", minute=0),
        id=_JOB_ID,
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("baseline scheduler started (06:00 and 18:00 UTC)")
    return scheduler


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    _scheduler.shutdown(wait=False)
    _scheduler = None
    logger.info("baseline scheduler stopped")
