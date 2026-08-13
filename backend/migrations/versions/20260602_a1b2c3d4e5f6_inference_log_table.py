"""inference_logs — full LLM input/output capture for auditability

Revision ID: a1b2c3d4e5f6
Revises: 4f8c7d1a3e92
Create Date: 2026-06-02 12:00:00.000000

Backs :class:`app.models.inference_log.InferenceLog`. One row per LLM call,
storing the exact prompt messages, raw response, fallback ``method`` taken,
and any Guardrails-Orchestrator detections — the prerequisite for TrustyAI
explainability and for auditing the agent's reasoning against production VMs.

Append-only by convention (no update/delete endpoints), matching the
``audit_logs`` posture. No enums — every categorical column is a String so new
``operation`` / ``method`` values land without an enum migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "4f8c7d1a3e92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inference_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("backend_type", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), server_default="", nullable=False),
        sa.Column("method", sa.String(length=48), server_default="llm", nullable=False),
        sa.Column(
            "input_messages",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("output_text", sa.Text(), nullable=True),
        sa.Column(
            "detections",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("verdict", sa.String(length=16), nullable=True),
        sa.Column("latency_ms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.Integer(), nullable=True),
        sa.Column("vm_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["vm_id"], ["vms.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("inference_logs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_inference_logs_op_time", ["operation", "created_at"], unique=False
        )
        batch_op.create_index("ix_inference_logs_time", ["created_at"], unique=False)
        batch_op.create_index("ix_inference_logs_method", ["method"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("inference_logs", schema=None) as batch_op:
        batch_op.drop_index("ix_inference_logs_method")
        batch_op.drop_index("ix_inference_logs_time")
        batch_op.drop_index("ix_inference_logs_op_time")
    op.drop_table("inference_logs")
