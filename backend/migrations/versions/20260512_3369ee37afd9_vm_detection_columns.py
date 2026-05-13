"""vm_detection_columns

Revision ID: 3369ee37afd9
Revises: 4963cfaf09eb
Create Date: 2026-05-12 22:05:25.470563

Adds the columns the environment detection cascade reads at tiers 2-4:
  - ``vsphere_cluster`` — RVTools "Cluster" column.
  - ``vsphere_folder`` — RVTools "Folder" column.
  - ``custom_attributes`` — RVTools "Custom Attributes" parsed as JSON.
  - ``environment_source`` — provenance ("unset" | "auto_detected" |
    "user_set") so redetect jobs can skip operator-supplied labels.

Backfill: existing VMs with non-null ``environment`` get
``environment_source="auto_detected"`` so the redetect-by-default
flow doesn't clobber labels imported under the previous schema.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3369ee37afd9"
down_revision: str | Sequence[str] | None = "4963cfaf09eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # JSON portability: SQLite gets sa.JSON; Postgres gets JSONB via
    # the with_variant override. Same pattern as the existing
    # vsphere_networks / vsphere_datastores columns.
    custom_attributes_type = sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()), "postgresql"
    )

    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("vsphere_cluster", sa.String(length=255), nullable=True),
        )
        batch_op.add_column(
            sa.Column("vsphere_folder", sa.String(length=512), nullable=True),
        )
        # ``server_default="{}"`` so existing rows get an empty dict on
        # the upgrade, then ``nullable=False`` after the backfill is
        # implicit. Using a server-side default keeps the upgrade
        # transactional on Postgres without a separate UPDATE pass.
        batch_op.add_column(
            sa.Column(
                "custom_attributes",
                custom_attributes_type,
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )
        batch_op.add_column(
            sa.Column(
                "environment_source",
                sa.String(length=32),
                server_default="unset",
                nullable=False,
            ),
        )

    # Backfill environment_source for existing rows: VMs that already
    # have a non-null environment must have been labeled somehow, so
    # tag them auto_detected. This protects them from accidental
    # overwrite if a redetect-with-force flag is used later — the
    # operator can still distinguish "labeled before the migration"
    # from "labeled by the user via PATCH".
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE vms SET environment_source = 'auto_detected' "
            "WHERE environment IS NOT NULL AND environment != ''"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.drop_column("environment_source")
        batch_op.drop_column("custom_attributes")
        batch_op.drop_column("vsphere_folder")
        batch_op.drop_column("vsphere_cluster")
