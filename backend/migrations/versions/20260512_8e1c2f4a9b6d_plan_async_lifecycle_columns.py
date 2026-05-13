"""plan_async_lifecycle_columns

Revision ID: 8e1c2f4a9b6d
Revises: 3369ee37afd9
Create Date: 2026-05-12 22:55:00.000000

Adds the async-lifecycle columns the rewritten POST /api/plans uses
to surface generation progress without an in-memory task store:

  - ``status`` — pending|validating|chunking|llm_grouping|assembling
                  |complete|failed (plain string, not an enum, so new
                  stages don't need a migration).
  - ``progress_message`` — short human-readable line for the UI poll.
  - ``progress_percent`` — 0-100, drives the UI progress bar.
  - ``error_message`` — verbatim str(e) from the typed-exception layer
                        so the failure surface on the plan detail page
                        matches what's in pod logs.
  - ``started_at`` / ``completed_at`` — wall-clock bracket; either may
                                       be null while running.

Backfill: every existing plan row predates the async-lifecycle work
and is by definition already done, so we mark them ``complete`` with
``progress_percent=100`` and stamp ``started_at`` / ``completed_at``
to ``created_at``. The dashboard will list them in the "complete"
bucket on next render and the migration is reversible without data
loss.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8e1c2f4a9b6d"
down_revision: str | Sequence[str] | None = "3369ee37afd9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "status",
                sa.String(length=32),
                server_default="pending",
                nullable=False,
            ),
        )
        batch_op.add_column(
            sa.Column("progress_message", sa.String(length=255), nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "progress_percent",
                sa.Integer(),
                server_default="0",
                nullable=False,
            ),
        )
        batch_op.add_column(
            sa.Column("error_message", sa.Text(), nullable=True),
        )
        batch_op.add_column(
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        )
        batch_op.add_column(
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )

    # Existing plans predate the async flow — they all finished
    # successfully (otherwise they wouldn't be in the table). Stamp
    # them complete so the new UI doesn't render them as "pending
    # forever".
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE migration_plans "
            "SET status = 'complete', "
            "    progress_percent = 100, "
            "    started_at = created_at, "
            "    completed_at = created_at"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.drop_column("completed_at")
        batch_op.drop_column("started_at")
        batch_op.drop_column("error_message")
        batch_op.drop_column("progress_percent")
        batch_op.drop_column("progress_message")
        batch_op.drop_column("status")
