"""baseline + validation engine tables (wave-scoped, deterministic)

Revision ID: 9d4a8c2b1f7e
Revises: 6e216aa1ffcf
Create Date: 2026-05-14 12:00:00.000000

Adds the schema backing the new wave-scoped baseline + validation flow:

  - ``ssh_keys`` — multi-key catalog for the per-plan validation keys.
    Coexists with the singleton appliance key on the PVC; the singleton
    is unaffected.
  - ``baseline_runs`` / ``baselines`` — one async run per wave; one
    Baseline row per VM in the wave.
  - ``validation_runs`` / ``vm_validations`` — same shape for the
    post-migration validation pass, plus a structured diff per VM.

All four new enums use ``names == values`` *except* ``vm_validation_verdict``
which intentionally uses ``passed="pass" / warned="warn" / failed="fail"``
to keep API output short ("pass" reads better in UI than "passed"). That
enum is declared with explicit value literals here so SQLAlchemy's
``values_callable`` on the column matches what's in ``CREATE TYPE``.

Wave reference is the composite ``(plan_id, wave_number)``; we deliberately
do NOT introduce a Wave table — waves remain JSON on
``MigrationPlan.waves`` (positional). When/if a Wave table eventually
lands, a follow-up migration can add a derived ``wave_id`` column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "9d4a8c2b1f7e"
down_revision: str | Sequence[str] | None = "6e216aa1ffcf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Enums referenced by table columns. Each enum is used in EXACTLY one
# table; ``op.create_table`` emits CREATE TYPE before CREATE TABLE
# automatically, so we don't need (and must not duplicate) the explicit
# ``enum.create()`` step. The first attempt at this migration did both,
# which is fine on SQLite (no real CREATE TYPE) but causes Postgres to
# emit CREATE TYPE twice inside one transaction — the second emission
# fails with DuplicateObject. Keep this comment so the next person who
# adds an enum to this migration doesn't reintroduce the loop.
SSH_KEY_STATUS = sa.Enum("active", "retired", name="ssh_key_status")
BASELINE_RUN_STATUS = sa.Enum(
    "pending", "running", "completed", "failed", name="baseline_run_status"
)
VM_COLLECTION_STATUS = sa.Enum(
    "pending", "in_progress", "captured", "failed", name="vm_collection_status"
)
VALIDATION_RUN_STATUS = sa.Enum(
    "pending", "running", "completed", "failed", name="validation_run_status"
)
# Deliberate name != value mapping (see model docstring). Values must match
# what the ORM column's ``values_callable`` returns.
VM_VALIDATION_VERDICT = sa.Enum(
    "pass", "warn", "fail", "unreachable", name="vm_validation_verdict"
)

_PG_ENUMS = (
    SSH_KEY_STATUS,
    BASELINE_RUN_STATUS,
    VM_COLLECTION_STATUS,
    VALIDATION_RUN_STATUS,
    VM_VALIDATION_VERDICT,
)


def upgrade() -> None:
    # NOTE: Don't add an explicit ``enum.create()`` loop here. See the
    # module-level enum comment for why.
    op.create_table(
        "ssh_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("public_key", sa.Text(), nullable=False),
        sa.Column("private_key_path", sa.String(length=512), nullable=False),
        sa.Column("fingerprint", sa.String(length=128), nullable=False),
        sa.Column(
            "algorithm",
            sa.String(length=32),
            server_default="ed25519",
            nullable=False,
        ),
        sa.Column(
            "status",
            SSH_KEY_STATUS,
            server_default="active",
            nullable=False,
        ),
        sa.Column("plan_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["plan_id"], ["migration_plans.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("ssh_keys", schema=None) as batch_op:
        batch_op.create_index("ix_ssh_keys_status", ["status"], unique=False)
        batch_op.create_index("ix_ssh_keys_plan_id", ["plan_id"], unique=False)
        batch_op.create_index(
            "ix_ssh_keys_status_plan", ["status", "plan_id"], unique=False
        )

    op.create_table(
        "baseline_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("wave_number", sa.Integer(), nullable=False),
        sa.Column("ssh_key_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            BASELINE_RUN_STATUS,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("total_vms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("captured_vms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_vms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress_message", sa.String(length=256), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["migration_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ssh_key_id"], ["ssh_keys.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("baseline_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_baseline_runs_status", ["status"], unique=False
        )
        batch_op.create_index(
            "ix_baseline_runs_plan_wave", ["plan_id", "wave_number"], unique=False
        )

    op.create_table(
        "baselines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("baseline_run_id", sa.Integer(), nullable=False),
        sa.Column("vm_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            VM_COLLECTION_STATUS,
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "collected_data",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("probe_catalog_version", sa.String(length=64), nullable=True),
        sa.Column(
            "probes_run",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("host_key_fingerprint", sa.String(length=128), nullable=True),
        sa.Column("failure_category", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column("collection_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collection_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["baseline_run_id"], ["baseline_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["vm_id"], ["vms.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("baselines", schema=None) as batch_op:
        batch_op.create_index(
            "ix_baselines_baseline_run_id", ["baseline_run_id"], unique=False
        )
        batch_op.create_index("ix_baselines_vm_id", ["vm_id"], unique=False)
        batch_op.create_index("ix_baselines_status", ["status"], unique=False)
        batch_op.create_index(
            "ix_baselines_run_status", ["baseline_run_id", "status"], unique=False
        )

    op.create_table(
        "validation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("wave_number", sa.Integer(), nullable=False),
        sa.Column("ssh_key_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            VALIDATION_RUN_STATUS,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("total_vms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("passed_vms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("warned_vms", sa.Integer(), server_default="0", nullable=False),
        sa.Column("failed_vms", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "unreachable_vms", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("progress_message", sa.String(length=256), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["plan_id"], ["migration_plans.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ssh_key_id"], ["ssh_keys.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("validation_runs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_validation_runs_status", ["status"], unique=False
        )
        batch_op.create_index(
            "ix_validation_runs_plan_wave",
            ["plan_id", "wave_number"],
            unique=False,
        )

    op.create_table(
        "vm_validations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("validation_run_id", sa.Integer(), nullable=False),
        sa.Column("vm_id", sa.Integer(), nullable=False),
        sa.Column("baseline_id", sa.Integer(), nullable=True),
        sa.Column(
            "verdict",
            VM_VALIDATION_VERDICT,
            server_default="unreachable",
            nullable=False,
        ),
        sa.Column(
            "collected_data",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "diff_result",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column(
            "host_key_changed", sa.Boolean(), server_default="0", nullable=False
        ),
        sa.Column("failure_category", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column(
            "validated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["validation_run_id"], ["validation_runs.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["vm_id"], ["vms.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["baseline_id"], ["baselines.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("vm_validations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_vm_validations_validation_run_id",
            ["validation_run_id"],
            unique=False,
        )
        batch_op.create_index("ix_vm_validations_vm_id", ["vm_id"], unique=False)
        batch_op.create_index(
            "ix_vm_validations_verdict", ["verdict"], unique=False
        )
        batch_op.create_index(
            "ix_vm_validations_run_verdict",
            ["validation_run_id", "verdict"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("vm_validations", schema=None) as batch_op:
        batch_op.drop_index("ix_vm_validations_run_verdict")
        batch_op.drop_index("ix_vm_validations_verdict")
        batch_op.drop_index("ix_vm_validations_vm_id")
        batch_op.drop_index("ix_vm_validations_validation_run_id")
    op.drop_table("vm_validations")

    with op.batch_alter_table("validation_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_validation_runs_plan_wave")
        batch_op.drop_index("ix_validation_runs_status")
    op.drop_table("validation_runs")

    with op.batch_alter_table("baselines", schema=None) as batch_op:
        batch_op.drop_index("ix_baselines_run_status")
        batch_op.drop_index("ix_baselines_status")
        batch_op.drop_index("ix_baselines_vm_id")
        batch_op.drop_index("ix_baselines_baseline_run_id")
    op.drop_table("baselines")

    with op.batch_alter_table("baseline_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_baseline_runs_plan_wave")
        batch_op.drop_index("ix_baseline_runs_status")
    op.drop_table("baseline_runs")

    with op.batch_alter_table("ssh_keys", schema=None) as batch_op:
        batch_op.drop_index("ix_ssh_keys_status_plan")
        batch_op.drop_index("ix_ssh_keys_plan_id")
        batch_op.drop_index("ix_ssh_keys_status")
    op.drop_table("ssh_keys")

    if dialect == "postgresql":
        for enum in reversed(_PG_ENUMS):
            enum.drop(bind, checkfirst=True)
