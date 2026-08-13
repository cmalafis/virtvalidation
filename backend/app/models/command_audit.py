"""Per-command SSH audit trail — exactly what the agent ran on each host.

Because the validation agent SSHes into **production** servers, operators need
a precise, immutable record of every command executed: which host, which run,
the exact command string, its exit status, how long it took, and a hash +
truncated copy of stdout so the output can be verified without storing
potentially large or sensitive payloads in full.

One row per command. Written from the orchestrator thread (the SSH collector
accumulates records in-memory per VM and they ride the existing
``CollectionResult`` channel to the DB, the same way ``probes_run`` and
``host_key_fingerprint`` do — the collector itself stays DB-free). Append-only
by convention, matching ``audit_logs`` / ``inference_logs``.

Note: this includes the read-only OS-detection probes (``cat /etc/os-release``,
the PowerShell ``Win32_OperatingSystem`` probe) — desirable, since the audit
should show *everything* the agent touched on the host.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class CommandAudit(Base):
    __tablename__ = "command_audits"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Which VM / host the command ran against. vm_id is a soft link (SET NULL)
    # so deleting a VM doesn't erase the historical audit of what ran on it.
    vm_id: Mapped[int | None] = mapped_column(
        ForeignKey("vms.id", ondelete="SET NULL"), nullable=True
    )
    host: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    # Which run drove the command — "baseline" / "validation" — and its id, so
    # operators can pull "every command run during baseline run 42".
    run_type: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    command: Mapped[str] = mapped_column(Text, nullable=False)
    exit_status: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # stdout integrity + a bounded copy. Full bytes are NOT stored — just the
    # length, a SHA-256 of the raw bytes (so an operator can prove what was
    # returned without us retaining it), and a truncated text preview.
    stdout_byte_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    stdout_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stdout_truncated: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_ms: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # True when the command was refused by the read-only gate before execution
    # (a security-relevant event) rather than actually run.
    blocked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_command_audits_run", "run_type", "run_id"),
        Index("ix_command_audits_vm", "vm_id"),
        Index("ix_command_audits_time", "started_at"),
    )
