"""add ssh_host_key_policy to app_settings

Persists the SSH host-key verification policy alongside the LLM model
and schedule preset. Default is ``auto_accept`` (TOFU) — federal customers
who need strict-mode verification flip it to ``strict`` from the Settings
page.

Revision ID: 0003_ssh_host_key_policy
Revises: 0002_inventory_metadata
Create Date: 2026-04-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_ssh_host_key_policy"
down_revision: str | Sequence[str] | None = "0002_inventory_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Postgres needs the enum type created up front; SQLite ignores it
    # and stores the value as a string. batch_alter_table handles both.
    policy_enum = sa.Enum("auto_accept", "strict", name="ssh_host_key_policy")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        policy_enum.create(bind, checkfirst=True)

    with op.batch_alter_table("app_settings") as batch:
        batch.add_column(
            sa.Column(
                "ssh_host_key_policy",
                policy_enum,
                nullable=False,
                server_default="auto_accept",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("app_settings") as batch:
        batch.drop_column("ssh_host_key_policy")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="ssh_host_key_policy").drop(bind, checkfirst=True)
