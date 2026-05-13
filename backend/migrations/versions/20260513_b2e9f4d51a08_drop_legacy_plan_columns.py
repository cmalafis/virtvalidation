"""drop legacy plan columns + plan_chunks table

Revision ID: b2e9f4d51a08
Revises: a17c4e8b9d12
Create Date: 2026-05-13 10:00:00.000000

Drops the columns + table that the legacy strategy-driven generation
path owned. The new pipeline writes structured per-wave annotation
into the existing ``waves`` JSON column (description, risk_score,
risk_rationale, notable_concerns, concurrency_group_id, method), so
nothing downstream consumes the dropped fields.

Columns removed from migration_plans:
  - generation_prompt
  - generation_response
  - plan_summary
  - rationale
  - warnings
  - next_actions
  - supersedes_plan_id (FK)
  - revision_number

Tables removed:
  - plan_chunks (hierarchical chunker artifact)

The ``planning_strategies`` table itself stays — strategy CRUD is
orthogonal to plan generation after the rearchitecture (operators
keep the strategy catalog UI for a future feature; plans no longer
reference one).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2e9f4d51a08"
down_revision: str | Sequence[str] | None = "a17c4e8b9d12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Drop plan_chunks first so the FK on plan_chunks.plan_id releases
    # migration_plans cleanly.
    existing_tables = set(inspector.get_table_names())
    if "plan_chunks" in existing_tables:
        op.drop_table("plan_chunks")

    plans_fks = inspector.get_foreign_keys("migration_plans")
    plans_indexes = inspector.get_indexes("migration_plans")
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        # Drop the self-FK on supersedes_plan_id (constraint name varies;
        # find by referenced table).
        for fk in plans_fks:
            name = fk.get("name")
            if name and fk.get("referred_table") == "migration_plans":
                batch_op.drop_constraint(name, type_="foreignkey")
        for ix in plans_indexes:
            if ix.get("name") and "supersedes_plan_id" in ix["name"]:
                batch_op.drop_index(ix["name"])
        for col in (
            "supersedes_plan_id",
            "revision_number",
            "generation_prompt",
            "generation_response",
            "plan_summary",
            "rationale",
            "warnings",
            "next_actions",
        ):
            batch_op.drop_column(col)


def downgrade() -> None:
    """Reverse: re-add the columns as nullable (data not restored).

    plan_chunks is also re-created with the same shape so the test
    harness can walk back to an earlier revision. Operational
    rollback isn't a goal — the audit log is the source of truth
    for the dropped values.
    """
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("next_actions", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("warnings", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("rationale", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("plan_summary", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("generation_response", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("generation_prompt", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "revision_number",
                sa.Integer(),
                server_default="1",
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("supersedes_plan_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_migration_plans_supersedes_plan_id",
            "migration_plans",
            ["supersedes_plan_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.create_table(
        "plan_chunks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("chunk_id", sa.String(length=255), nullable=False),
        sa.Column("sequence_index", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("reason_for_chunk", sa.String(length=512), nullable=False),
        sa.Column("partition_key", sa.JSON(), nullable=False),
        sa.Column("sub_key", sa.JSON(), nullable=False),
        sa.Column("hints", sa.JSON(), nullable=False),
        sa.Column("vm_ids", sa.JSON(), nullable=False),
        sa.Column("sequence_dependencies", sa.JSON(), nullable=False),
        sa.Column("chunk_rationale", sa.Text(), nullable=True),
        sa.Column("chunk_risk_level", sa.String(length=32), nullable=True),
        sa.Column("wave_numbers", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["migration_plans.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Indexes mirrored from the original migration so older downgrades
    # past this point can drop them by their original names.
    with op.batch_alter_table("plan_chunks", schema=None) as batch_op:
        batch_op.create_index("ix_plan_chunks_plan_id", ["plan_id"], unique=False)
        batch_op.create_index(
            "ix_plan_chunks_plan_seq",
            ["plan_id", "sequence_index"],
            unique=False,
        )
