"""plan series and excluded vms

Revision ID: bea67743fa74
Revises: c1d2e3f4a5b6
Create Date: 2026-09-29 09:43:53.732040

``POST /api/plans`` no longer caps the selection at ``max_vms_per_plan``.
A larger selection is generated as a *series* of plans, each holding at
most that many VMs (``app.core.plan_chunking``). Four columns on
``migration_plans`` describe the series:

  * ``series_id``    — shared by every plan row created by one POST
  * ``series_index`` — 1-based position in the series
  * ``series_total`` — number of rows in the series
  * ``excluded_vms`` — ``[{vm_id, vm_name, reason}]`` the operator
                        selected but the create call left out
                        (assessment-blocked / unresolvable target)

All nullable; rows that pre-date batching read as NULL and the API
serialises them as ``None`` / ``[]``.

Autogenerate also proposed an unrelated ``app_settings.active_llm_backend``
enum retype and a ``vms`` index rename that reflect pre-existing model /
migration drift. Both were dropped from this revision on purpose — they
are not part of this change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "bea67743fa74"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("series_id", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("series_index", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("series_total", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "excluded_vms",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=True,
            )
        )
        batch_op.create_index(
            batch_op.f("ix_migration_plans_series_id"), ["series_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_migration_plans_series_id"))
        batch_op.drop_column("excluded_vms")
        batch_op.drop_column("series_total")
        batch_op.drop_column("series_index")
        batch_op.drop_column("series_id")
