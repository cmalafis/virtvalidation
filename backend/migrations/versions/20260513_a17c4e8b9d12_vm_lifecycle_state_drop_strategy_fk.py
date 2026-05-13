"""vm_lifecycle_state + drop migration_plans.strategy_id

Revision ID: a17c4e8b9d12
Revises: b41a7f29c8d2
Create Date: 2026-05-13 09:00:00.000000

Adds the plan-membership lifecycle column to VMs and drops the now-
inert ``strategy_id`` FK on ``migration_plans``.

VM lifecycle (parallel to ``status``):
  - ``available`` — discovered, not in any plan (selector default)
  - ``planned``    — committed to an active plan; hidden from selector
  - ``migrated``   — operator declared the cutover done
  - ``rolled_back``— manually reverted from migrated (back-on-VMware)
  - ``unmanageable`` — informational; not produced by transitions

Backfill: any VM referenced in ``migration_plans.vm_ids`` of a plan
whose status is NOT in (complete, failed) is marked ``planned``.
All other VMs default to ``available``. We deliberately do not mark
anything ``migrated`` — that requires operator action going forward.

Strategy FK drop: the new pipeline doesn't consult PlanningStrategy
for structural decisions (deterministic Python decides waves). The
``planning_strategies`` table + CRUD endpoints stay so the strategy
catalog isn't lost, but no plan references one. Existing rows lose
their strategy linkage; we record the prior strategy_id values in
the audit log before dropping the column for federal traceability.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a17c4e8b9d12"
down_revision: str | Sequence[str] | None = "b41a7f29c8d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    dialect = bind.dialect.name

    # ------------------------------------------------------------------
    # 1. Add VM lifecycle columns.
    # ------------------------------------------------------------------
    vm_lifecycle_enum = sa.Enum(
        "available",
        "planned",
        "migrated",
        "rolled_back",
        "unmanageable",
        name="vm_lifecycle_state",
    )
    # On PostgreSQL, Enum types must be created before column use; on
    # SQLite they're rendered as VARCHAR + CHECK so create_type=False is
    # safe.
    if dialect == "postgresql":
        vm_lifecycle_enum.create(bind, checkfirst=True)

    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "lifecycle_state",
                vm_lifecycle_enum,
                server_default="available",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "lifecycle_state_changed_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )
        batch_op.create_index("ix_vms_lifecycle_state", ["lifecycle_state"], unique=False)

    # ------------------------------------------------------------------
    # 2. Backfill lifecycle_state from existing plan membership.
    #    Any VM in a plan whose status is NOT in (complete, failed) is
    #    "planned". Everything else stays "available".
    # ------------------------------------------------------------------
    plans_rows = bind.execute(
        sa.text(
            "SELECT id, vm_ids, status FROM migration_plans "
            "WHERE status NOT IN ('complete', 'failed', 'migrated')"
        )
    ).fetchall()

    planned_vm_ids: set[int] = set()
    for row in plans_rows:
        raw = row.vm_ids
        if raw is None:
            continue
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        if isinstance(raw, str):
            try:
                ids = json.loads(raw)
            except json.JSONDecodeError:
                ids = []
        else:
            ids = raw
        for vid in ids or []:
            try:
                planned_vm_ids.add(int(vid))
            except (TypeError, ValueError):
                continue

    if planned_vm_ids:
        # SQLite has a parameter-binding cap; chunk to be safe.
        ids_list = list(planned_vm_ids)
        chunk = 500
        for i in range(0, len(ids_list), chunk):
            batch = ids_list[i : i + chunk]
            placeholders = ",".join(f":id{j}" for j in range(len(batch)))
            params = {f"id{j}": vid for j, vid in enumerate(batch)}
            bind.execute(
                sa.text(
                    f"UPDATE vms SET lifecycle_state = 'planned' " f"WHERE id IN ({placeholders})"
                ),
                params,
            )

    # ------------------------------------------------------------------
    # 3. Audit-log the strategy_id values we're about to drop, then drop
    #    the FK + column.
    # ------------------------------------------------------------------
    strategy_rows = bind.execute(
        sa.text("SELECT id, strategy_id FROM migration_plans " "WHERE strategy_id IS NOT NULL")
    ).fetchall()
    for row in strategy_rows:
        bind.execute(
            sa.text(
                "INSERT INTO audit_logs (action, actor, resource_type, "
                "resource_id, details) "
                "VALUES (:action, :actor, :rtype, :rid, :details)"
            ),
            {
                "action": "plan.strategy_decoupled",
                "actor": "system",
                "rtype": "plan",
                "rid": str(row.id),
                "details": json.dumps(
                    {
                        "prior_strategy_id": row.strategy_id,
                        "reason": "Strategy FK dropped in pipeline rearchitecture; "
                        "structural decisions are now deterministic Python.",
                    }
                ),
            },
        )

    plans_indexes = {ix["name"] for ix in inspector.get_indexes("migration_plans")}
    plans_fks = {
        fk["name"] for fk in inspector.get_foreign_keys("migration_plans") if fk.get("name")
    }

    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        # Drop FK explicitly before column so SQLite + Postgres both
        # handle it cleanly. The constraint name varies across engines;
        # rely on the inspector to find it.
        for fk_name in list(plans_fks):
            if fk_name and "strategy" in fk_name:
                batch_op.drop_constraint(fk_name, type_="foreignkey")
        # SQLite doesn't index FKs by default; only drop if present.
        for ix_name in plans_indexes:
            if ix_name and "strategy_id" in ix_name:
                batch_op.drop_index(ix_name)
        batch_op.drop_column("strategy_id")


def downgrade() -> None:
    """Reverse: re-add strategy_id as nullable, drop lifecycle columns.

    The reverse cannot restore the original strategy_id values; the
    audit log retains them. Downgrade exists to satisfy the migration
    test harness, not as an operational recovery path.
    """
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("strategy_id", sa.Integer(), nullable=True))
        # Re-create with the ORIGINAL constraint name so the older
        # downgrade (the one that originally created strategy_id) can
        # find it. Renaming the FK on downgrade would break the chain
        # back to base — the test harness exercises that path.
        batch_op.create_foreign_key(
            "fk_migration_plans_strategy_id",
            "planning_strategies",
            ["strategy_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.drop_index("ix_vms_lifecycle_state")
        batch_op.drop_column("lifecycle_state_changed_at")
        batch_op.drop_column("lifecycle_state")

    if dialect == "postgresql":
        sa.Enum(name="vm_lifecycle_state").drop(bind, checkfirst=True)
