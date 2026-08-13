"""command_audits — per-command SSH audit trail

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-02 12:30:00.000000

Backs :class:`app.models.command_audit.CommandAudit`. One row per command the
agent ran on a host (exit status, stdout hash + truncated preview, duration,
and whether the read-only gate blocked it) so operators have a precise,
immutable record of what touched each production server. Append-only by
convention.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_audits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("vm_id", sa.Integer(), nullable=True),
        sa.Column("host", sa.String(length=255), server_default="", nullable=False),
        sa.Column("run_type", sa.String(length=16), server_default="", nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("exit_status", sa.Integer(), nullable=True),
        sa.Column("stdout_byte_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("stdout_sha256", sa.String(length=64), nullable=True),
        sa.Column("stdout_truncated", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("blocked", sa.Boolean(), server_default="0", nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["vm_id"], ["vms.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("command_audits", schema=None) as batch_op:
        batch_op.create_index("ix_command_audits_run", ["run_type", "run_id"], unique=False)
        batch_op.create_index("ix_command_audits_vm", ["vm_id"], unique=False)
        batch_op.create_index("ix_command_audits_time", ["started_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("command_audits", schema=None) as batch_op:
        batch_op.drop_index("ix_command_audits_time")
        batch_op.drop_index("ix_command_audits_vm")
        batch_op.drop_index("ix_command_audits_run")
    op.drop_table("command_audits")
