"""llm_backend_type enum: add 'trustyai'

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-02 13:30:00.000000

Adds the ``trustyai`` value to the Postgres ``llm_backend_type`` enum so the
TrustyAI Guardrails Orchestrator backend can be selected as the active
backend.

Postgres specifics:
  - ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction block on all
    supported PG versions, so we use Alembic's ``autocommit_block``.
  - ``IF NOT EXISTS`` makes the migration idempotent.
  - There is NO way to drop an enum value in Postgres, so ``downgrade`` is a
    documented no-op. The value lingering is harmless — nothing references it
    once the operator switches away from the trustyai backend.

SQLite (the test DB) has no enum type — the column is TEXT — so the whole
operation is a no-op there.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite / others: enum is plain text, nothing to alter.
        return
    # ADD VALUE must run outside a transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE llm_backend_type ADD VALUE IF NOT EXISTS 'trustyai'")


def downgrade() -> None:
    # Postgres cannot drop an enum value; leaving 'trustyai' in place is
    # harmless. Intentional no-op.
    pass
