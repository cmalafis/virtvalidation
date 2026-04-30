"""add inventory metadata fields to vms

Adds ssh_port, current_platform, environment, owner — the free-form
inventory metadata captured by the manual enrollment form and the CSV
import schema. These were already columns in the canonical CSV template
but the model silently dropped them; this migration closes the gap.

Revision ID: 0002_inventory_metadata
Revises: 0001_mtv_fields
Create Date: 2026-04-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_inventory_metadata"
down_revision: str | Sequence[str] | None = "0001_mtv_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("vms") as batch:
        batch.add_column(
            sa.Column(
                "ssh_port",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("22"),
            )
        )
        batch.add_column(sa.Column("current_platform", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("environment", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("owner", sa.String(length=128), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("vms") as batch:
        batch.drop_column("owner")
        batch.drop_column("environment")
        batch.drop_column("current_platform")
        batch.drop_column("ssh_port")
