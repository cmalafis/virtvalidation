"""plan mapping_id

Revision ID: bdcc6835d70a
Revises: 8f96c847dcaf
Create Date: 2026-05-05 21:53:08.551500

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bdcc6835d70a"
down_revision: str | Sequence[str] | None = "8f96c847dcaf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mapping_id", sa.Integer(), nullable=True))
        # Named so batch-mode downgrade on SQLite can drop it by name.
        batch_op.create_foreign_key(
            "fk_migration_plans_mapping_id",
            "resource_mappings",
            ["mapping_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.drop_constraint("fk_migration_plans_mapping_id", type_="foreignkey")
        batch_op.drop_column("mapping_id")
