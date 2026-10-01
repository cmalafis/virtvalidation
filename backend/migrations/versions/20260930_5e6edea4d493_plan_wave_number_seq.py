"""plan wave number seq

Revision ID: 5e6edea4d493
Revises: bea67743fa74
Create Date: 2026-09-30 21:01:04.586229

Operators can now rearrange a generated plan (``POST /api/plans/{id}/edits``):
move VMs between waves, into a new wave, into another plan, or out of the
plan. Baseline / validation runs and MTV CR names key on the wave number,
so an edit never renumbers a wave — and must never hand a dropped wave's
number to a new one, or the new wave would inherit the old one's runs and
CR name.

``migration_plans.wave_number_seq`` is the high-water mark that guarantees
it: a wave created by an edit is numbered
``max(wave_number_seq, highest current wave) + 1``. Nullable; NULL means
the plan has never been edited, and no backfill is needed.

Autogenerate also proposed an unrelated ``app_settings.active_llm_backend``
enum retype and a ``vms`` index rename that reflect pre-existing model /
migration drift. Both were dropped from this revision on purpose — they
are not part of this change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e6edea4d493"
down_revision: str | Sequence[str] | None = "bea67743fa74"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.add_column(sa.Column("wave_number_seq", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("migration_plans", schema=None) as batch_op:
        batch_op.drop_column("wave_number_seq")
