"""add MTV mapping fields to vms

Adds the source vSphere context (networks, datastores) and the destination
OpenShift Virtualization context (namespace, storage class, network attachment
definition) needed by the MTV plan generator.

Revision ID: 0001_mtv_fields
Revises:
Create Date: 2026-04-29

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001_mtv_fields"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Same JSON-with-postgres-variant the application models use.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("vms") as batch:
        batch.add_column(
            sa.Column(
                "vsphere_networks",
                JSONType,
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(
            sa.Column(
                "vsphere_datastores",
                JSONType,
                nullable=False,
                server_default=sa.text("'[]'"),
            )
        )
        batch.add_column(sa.Column("target_namespace", sa.String(length=253), nullable=True))
        batch.add_column(sa.Column("target_storage_class", sa.String(length=253), nullable=True))
        batch.add_column(
            sa.Column("target_network_attachment", sa.String(length=253), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("vms") as batch:
        batch.drop_column("target_network_attachment")
        batch.drop_column("target_storage_class")
        batch.drop_column("target_namespace")
        batch.drop_column("vsphere_datastores")
        batch.drop_column("vsphere_networks")
