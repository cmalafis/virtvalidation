"""Server-side inventory import jobs.

One row per uploaded RVTools workbook / CSV. The row is the durable
progress record the UI polls: the parse + upsert runs in a BackgroundTask,
commits per batch, and updates the counters here as it goes, so a late
failure never discards completed work and a restart leaves an honest
record (``app.core.startup.fail_orphan_imports`` marks in-flight rows
failed).

``status`` is a plain string (not a PG enum) for the same reason
``MigrationPlan.status`` is: stages get added, and a CREATE TYPE migration
per stage is noise.

    uploaded → scanning → awaiting_mapping → importing → completed
                                                       ↘ failed | cancelled

``awaiting_mapping`` is where the operator routes each vCenter hostname
detected in the file to a registered source. An upload that already
carries a mapping (or a default vCenter) skips straight to ``importing``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType

IMPORT_ACTIVE_STATUSES: tuple[str, ...] = ("uploaded", "scanning", "importing")
IMPORT_TERMINAL_STATUSES: tuple[str, ...] = ("completed", "failed", "cancelled")


class ImportJob(Base):
    __tablename__ = "import_jobs"

    # UUID4 string — the id is handed to the browser and polled, so it
    # shouldn't be a guessable sequence.
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="rvtools")
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Where the upload is spooled on local disk while the job runs.
    # Deleted on every terminal transition; never returned by the API.
    file_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="upsert")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="uploaded")
    current_sheet: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress_message: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Row counters. ``rows_total`` is NULL when the workbook doesn't
    # declare sheet dimensions (the UI then shows rows-read, no percent).
    rows_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_read: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_valid: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_warned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unchanged_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    marked_missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # [{hostname, vm_count, suggested_vcenter_id}] from the scan pass.
    detected_vcenters: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    # {normalized hostname: vcenter_source_id}
    vcenter_mapping: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    default_vcenter_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Completion rollups: per-vCenter counts, environment distribution,
    # which auxiliary sheets were found.
    result: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    error_message: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="user")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_import_jobs_status_created", "status", "created_at"),)


class ImportJobReject(Base):
    """One problem row. A table rather than JSON on the job so a badly
    malformed 5,000-row workbook can't bloat the polled job row.

    ``severity`` separates rows that were NOT imported (``rejected``) from
    rows imported with a caveat the operator should see (``warning`` — e.g.
    no datastore could be determined, so the VM can't be storage-mapped).
    Hiding a real VM from inventory because its export row is thin would
    be worse than importing it flagged.
    """

    __tablename__ = "import_job_rejects"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sheet: Mapped[str] = mapped_column(String(64), nullable=False)
    # 1-based spreadsheet row, header = row 1 — what the operator sees in Excel.
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    vm_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, default="rejected")
    reason: Mapped[str] = mapped_column(String(512), nullable=False)
