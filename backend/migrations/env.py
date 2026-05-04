"""Alembic environment.

Uses the same DATABASE_URL as the running app and pulls model metadata from
``app.core.db.Base`` so future ``--autogenerate`` runs see the full schema.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.core.config import settings as app_settings
from app.core.db import Base
from app.models import (  # noqa: F401  (register models on Base)
    audit,
    grouping,
    network_review,
    plan,
    settings,
    storage_review,
    validation,
    vcenter,
    vm,
)

config = context.config
# DATABASE_URL resolution order (first non-empty wins):
#   1. URL already set on the Config — lets test helpers and the
#      lifespan migration runner inject a per-call URL without
#      mutating env vars.
#   2. ALEMBIC_DATABASE_URL env var — explicit override for one-off
#      operator commands like ``alembic revision --autogenerate``
#      against a temporary SQLite file.
#   3. app.core.config.settings.database_url — the live deployment's
#      DATABASE_URL, the steady-state path.
import os as _os  # noqa: E402

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option(
        "sqlalchemy.url",
        _os.environ.get("ALEMBIC_DATABASE_URL") or app_settings.database_url,
    )

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        # render_as_batch keeps the migration runnable against SQLite
        # (used in tests) where many ALTERs are unsupported natively.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
