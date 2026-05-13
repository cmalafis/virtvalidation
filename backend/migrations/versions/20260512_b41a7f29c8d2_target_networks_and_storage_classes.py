"""target_networks and target_storage_classes

Revision ID: b41a7f29c8d2
Revises: 8e1c2f4a9b6d
Create Date: 2026-05-12 21:15:00.000000

Operator-defined catalog of target NetworkAttachmentDefinitions /
CUDNs / UDNs and target StorageClasses on each registered OCP target.
The mapping editor populates its target dropdowns from these tables —
live cluster discovery is intentionally not the source of truth so
air-gapped operators can declare what's available without granting
the appliance cluster API access.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b41a7f29c8d2"
down_revision: str | Sequence[str] | None = "8e1c2f4a9b6d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "target_networks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ocp_target_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "network_type",
            sa.Enum("nad", "cudn", "udn", "pod", name="target_network_type"),
            nullable=False,
        ),
        sa.Column("namespace", sa.String(length=128), nullable=True),
        sa.Column("is_default", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ocp_target_id"], ["ocp_targets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ocp_target_id", "name", name="uq_target_network_per_cluster"),
    )
    with op.batch_alter_table("target_networks", schema=None) as batch_op:
        batch_op.create_index(
            "ix_target_network_ocp_target",
            ["ocp_target_id"],
            unique=False,
        )

    op.create_table(
        "target_storage_classes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ocp_target_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column(
            "access_mode",
            sa.Enum(
                "ReadWriteOnce",
                "ReadWriteMany",
                "ReadOnlyMany",
                name="target_sc_access_mode",
            ),
            server_default="ReadWriteOnce",
            nullable=False,
        ),
        sa.Column("is_default", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["ocp_target_id"], ["ocp_targets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ocp_target_id", "name", name="uq_target_sc_per_cluster"),
    )
    with op.batch_alter_table("target_storage_classes", schema=None) as batch_op:
        batch_op.create_index(
            "ix_target_sc_ocp_target",
            ["ocp_target_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("target_storage_classes", schema=None) as batch_op:
        batch_op.drop_index("ix_target_sc_ocp_target")
    op.drop_table("target_storage_classes")
    with op.batch_alter_table("target_networks", schema=None) as batch_op:
        batch_op.drop_index("ix_target_network_ocp_target")
    op.drop_table("target_networks")
    # Postgres enum types are leftover after drop_table; drop them too
    # so a re-upgrade can recreate them. SQLite ignores these (no-op).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        sa.Enum(name="target_sc_access_mode").drop(bind, checkfirst=True)
        sa.Enum(name="target_network_type").drop(bind, checkfirst=True)
