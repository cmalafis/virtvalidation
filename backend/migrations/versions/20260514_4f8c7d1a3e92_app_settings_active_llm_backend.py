"""app_settings: active_llm_backend + last_llm_error fields

Revision ID: 4f8c7d1a3e92
Revises: 9d4a8c2b1f7e
Create Date: 2026-05-14 14:00:00.000000

Adds the runtime-switchable LLM backend selection to the singleton
``app_settings`` row, plus two columns for the visible-auth-failure
banner.

Seed safety
-----------
The default for ``active_llm_backend`` is ``mock`` so a brand-new DB
boots without a real LLM. EXISTING deployments running ollama/kserve
must NOT silently flip to mock on first restart after this migration.

The ``upgrade()`` function therefore:
  1. Reads the operator's ``LLM_BACKEND_TYPE`` env var (whatever they
     have today).
  2. Validates it against the enum's allowed values.
  3. UPSERTs the singleton ``app_settings`` row (id=1) with that
     value as ``active_llm_backend``.

After this migration runs once, the env var is irrelevant for
*selection*; it becomes pure deployment config (the bootstrap value
for fresh DBs only). Connection-config env vars (URLs, MaaS API key)
remain meaningful indefinitely.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4f8c7d1a3e92"
down_revision: str | Sequence[str] | None = "9d4a8c2b1f7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Mirrors LLMBackendType in app.core.llm.types. Hard-coded here so the
# migration doesn't import application code (Alembic best practice —
# migrations should run against ANY past or future revision of the
# codebase, including ones where the import would fail or the enum
# would have different members).
_BACKEND_VALUES = ("ollama", "kserve", "vllm", "mock", "maas")
_LLM_BACKEND_ENUM = sa.Enum(*_BACKEND_VALUES, name="llm_backend_type")


def _safe_seed_value() -> str:
    """Pick the bootstrap value for ``active_llm_backend`` based on
    the operator's existing env var. Defaults to ``mock`` if the env
    var is absent or invalid — never silently seeds a real backend
    that wasn't explicitly opted into."""
    raw = (os.environ.get("LLM_BACKEND_TYPE") or "").strip().lower()
    if raw in _BACKEND_VALUES:
        return raw
    return "mock"


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    # On Postgres, create the enum type explicitly so the column add
    # below can reference it. SQLite has no real enum types — the
    # column gets a CHECK constraint via Alembic and the explicit
    # CREATE TYPE is a no-op the dialect ignores.
    if dialect == "postgresql":
        _LLM_BACKEND_ENUM.create(bind, checkfirst=True)

    # The Postgres path can't add a NOT NULL enum column without a
    # default in a single statement when the table already has rows.
    # Add nullable first, backfill, then alter to NOT NULL.
    op.add_column(
        "app_settings",
        sa.Column(
            "active_llm_backend",
            _LLM_BACKEND_ENUM,
            nullable=True,
        ),
    )
    op.add_column(
        "app_settings",
        sa.Column("last_llm_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "app_settings",
        sa.Column(
            "last_llm_error_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )

    seed_value = _safe_seed_value()

    # Upsert the singleton row. If id=1 doesn't exist yet (truly fresh
    # DB), insert it; otherwise update the new column on the existing
    # row. Use a portable UPDATE-then-INSERT-if-no-row sequence — both
    # SQLite and Postgres handle it identically.
    bind.execute(
        sa.text("UPDATE app_settings SET active_llm_backend = :v WHERE id = 1"),
        {"v": seed_value},
    )
    # Insert id=1 if it's still missing. Both dialects accept this
    # form; we don't use ON CONFLICT because SQLite prior to 3.24 lacks
    # it and the test suite uses SQLite.
    has_row = bind.execute(sa.text("SELECT 1 FROM app_settings WHERE id = 1")).scalar()
    if not has_row:
        bind.execute(
            sa.text(
                "INSERT INTO app_settings (id, ollama_model, "
                "schedule_preset, ssh_host_key_policy, "
                "active_llm_backend) "
                "VALUES (1, 'llama3:8b', 'twice_daily', "
                "'auto_accept', :v)"
            ),
            {"v": seed_value},
        )

    # Now that every row has a value, tighten the column to NOT NULL
    # with a server_default so future inserts that omit the column
    # still get a sensible value.
    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.alter_column(
            "active_llm_backend",
            existing_type=_LLM_BACKEND_ENUM,
            nullable=False,
            server_default="mock",
        )


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    with op.batch_alter_table("app_settings", schema=None) as batch_op:
        batch_op.drop_column("last_llm_error_at")
        batch_op.drop_column("last_llm_error")
        batch_op.drop_column("active_llm_backend")

    if dialect == "postgresql":
        _LLM_BACKEND_ENUM.drop(bind, checkfirst=True)
