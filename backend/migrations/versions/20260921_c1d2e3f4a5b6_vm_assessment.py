"""vm migratability assessment

Revision ID: c1d2e3f4a5b6
Revises: b8926f29c9b9
Create Date: 2026-09-21 00:10:00.000000

``vms.assessment_status`` (blocked | warning | ok | unknown) and
``vms.assessment`` (findings + not-evaluated rules, from
``app.core.assessment``), plus ``vms.assessment_finding_ids`` for the
inventory's per-finding filter. Existing rows start ``unknown``; they are
assessed on their next import or by ``POST /api/assessment/run``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b8926f29c9b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "assessment_status",
                sa.String(length=16),
                server_default="unknown",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("assessment", json_type, server_default=sa.text("'{}'"), nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                "assessment_finding_ids",
                json_type,
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.drop_column("assessment_finding_ids")
        batch_op.drop_column("assessment")
        batch_op.drop_column("assessment_status")
