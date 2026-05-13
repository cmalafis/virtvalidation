"""migration_plans: add mapping_ids JSON column for multi-mapping plans

Revision ID: c3f1d92a7b04
Revises: b2e9f4d51a08
Create Date: 2026-05-13 14:00:00.000000

Resource mappings are scoped per source vCenter. A plan that spans
multiple vCenters needs multiple mappings — one per vcenter — and
each VM gets routed through the mapping whose
``vcenter_source_id`` matches its own. The legacy ``mapping_id``
singular column couldn't express that.

This migration adds ``mapping_ids`` (JSON list of ResourceMapping
ids) and backfills existing rows so a plan that previously had
``mapping_id=42`` becomes ``mapping_ids=[42]`` (and ``[]`` when
mapping_id was NULL). The singular column stays for one release —
new writes populate both so any reader that still consults
``mapping_id`` keeps working.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3f1d92a7b04"
down_revision: str | Sequence[str] | None = "b2e9f4d51a08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable column — matches the surrounding JSON columns on this
    # table (vm_ids / waves use Python-side defaults, no server_default).
    # Postgres JSONB rejects a plain-string server_default; SQLite's
    # JSON is more forgiving but we keep one shape for both.
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("mapping_ids", sa.JSON(), nullable=True))

    # Backfill existing rows: mapping_id populated → [mapping_id];
    # NULL → keep NULL (read sites already guard ``or []``). Dialect-
    # split because Postgres needs an explicit JSONB cast and the
    # builtin json_build_array; SQLite is happy with string
    # concatenation.
    bind = op.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        op.execute(
            "UPDATE migration_plans "
            "SET mapping_ids = jsonb_build_array(mapping_id) "
            "WHERE mapping_id IS NOT NULL AND mapping_ids IS NULL"
        )
    else:
        op.execute(
            "UPDATE migration_plans "
            "SET mapping_ids = '[' || mapping_id || ']' "
            "WHERE mapping_id IS NOT NULL AND mapping_ids IS NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.drop_column("mapping_ids")
