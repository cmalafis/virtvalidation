"""plan migration type

Revision ID: b8926f29c9b9
Revises: f95a29c7206a
Create Date: 2026-09-20 23:30:00.000000

``migration_plans.migration_type`` — the MTV ``Plan.spec.type`` emitted for
every wave of the plan (``cold`` | ``warm``). The generator used to
hardcode ``warm: true``, a deprecated field, which sends every VM down a
path that requires Changed Block Tracking and VMware Tools.

Existing plans get ``cold``. Their stored YAML was generated as warm; the
export endpoints regenerate from the plan row, so re-exported YAML for an
old plan will now say ``type: cold``. Set the column to ``warm`` for any
in-flight plan that was deliberately warm.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8926f29c9b9"
down_revision: str | Sequence[str] | None = "f95a29c7206a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "migration_type", sa.String(length=16), server_default="cold", nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.drop_column("migration_type")
