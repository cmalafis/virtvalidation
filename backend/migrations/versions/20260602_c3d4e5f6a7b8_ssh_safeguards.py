"""ssh safeguards: kill-switch + run authorization fields

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-02 13:00:00.000000

Production-server safeguards:

  - ``app_settings.ssh_operations_enabled`` — global kill-switch (default
    True). When False the API refuses to start any baseline/validation run.
  - ``baseline_runs`` / ``validation_runs`` gain ``authorized_by`` +
    ``authorization_reason`` — who authorized running against
    production/classified hosts, and why.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "ssh_operations_enabled",
                sa.Boolean(),
                server_default="1",
                nullable=False,
            )
        )
    for table in ("baseline_runs", "validation_runs"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("authorized_by", sa.String(length=255), nullable=True))
            batch_op.add_column(sa.Column("authorization_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    for table in ("baseline_runs", "validation_runs"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("authorization_reason")
            batch_op.drop_column("authorized_by")
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_column("ssh_operations_enabled")
