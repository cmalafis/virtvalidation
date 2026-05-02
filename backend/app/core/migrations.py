"""Alembic migration orchestration for the FastAPI lifespan.

Two responsibilities:

  - **Apply pending migrations** at startup (``alembic upgrade head``).
    The app fails fast on a migration error rather than serving 500s
    against a half-migrated schema.

  - **Bridge legacy ``Base.metadata.create_all`` deployments** to the
    Alembic-managed world. If the database already has application
    tables but no ``alembic_version`` row, we stamp it at the current
    head so the upgrade is a no-op and history starts tracking from
    here. Without this bridge, every v0.1.x deployment would crash on
    upgrade because Alembic would try to ``CREATE TABLE`` over the
    existing tables.

This module is small on purpose. The migration scripts themselves
live in ``backend/migrations/versions/`` and are the source of truth
for schema. Everything here is wiring.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# Tables we create. If ANY of these are present without an
# alembic_version row, we treat the DB as "legacy create_all" and
# stamp head before upgrading. The list lives here (not pulled from
# Base.metadata) so a future model rename doesn't accidentally break
# the bridge logic against an older deployment that still has the
# old name.
_BRIDGE_TABLES = (
    "vms",
    "baseline_snapshots",
    "validation_results",
    "migration_plans",
    "audit_logs",
    "app_settings",
    "network_design_reviews",
    "vcenter_sources",
    "vm_groups",
    "vm_group_members",
    "migration_programs",
)


class MigrationError(RuntimeError):
    """Raised when migrations fail to apply. Caller should crash the
    process — running with a half-migrated schema is worse than not
    running at all."""


def _alembic_config() -> Config:
    """Build an Alembic ``Config`` rooted at ``backend/alembic.ini``.

    The ini file is read relative to the working directory the app
    starts in (``/app`` inside the container, ``backend/`` for local
    uvicorn). The ``script_location`` is resolved against the ini
    file's directory regardless.
    """
    repo_root = Path(__file__).resolve().parents[2]
    ini_path = repo_root / "alembic.ini"
    if not ini_path.is_file():
        raise MigrationError(f"alembic.ini not found at {ini_path}")
    cfg = Config(str(ini_path))
    # Ensure migrations/ resolves relative to the ini file even when
    # the process CWD is somewhere else.
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    return cfg


def _has_alembic_version(engine: Engine) -> bool:
    return "alembic_version" in inspect(engine).get_table_names()


def _has_legacy_tables(engine: Engine) -> bool:
    """Return True iff the DB has at least one application table but
    no alembic_version table — the v0.1.x ``create_all`` shape."""
    if _has_alembic_version(engine):
        return False
    existing = set(inspect(engine).get_table_names())
    return any(t in existing for t in _BRIDGE_TABLES)


def _current_head(cfg: Config) -> str:
    """Return the head revision id from the migrations directory."""
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    if head is None:
        raise MigrationError(
            "No migrations found in migrations/versions/. "
            "At least the baseline migration must be present."
        )
    return head


def _current_revision(engine: Engine) -> str | None:
    """Read the alembic_version row (if any) directly so we don't
    depend on alembic command output."""
    if not _has_alembic_version(engine):
        return None
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        return ctx.get_current_revision()


def schema_status(engine: Engine) -> dict[str, Any]:
    """Snapshot of the database's migration state.

    Surfaced via ``GET /api/health/schema`` so operators can confirm
    "is the DB at the right revision" without shelling into the
    container. Returns:

      - ``current_revision``    — what the DB thinks it's at
      - ``head_revision``       — what the migrations dir says is latest
      - ``is_up_to_date``       — bool
      - ``pending_migrations``  — list of revision ids between current → head
      - ``legacy_create_all``   — True when bridge logic would fire
    """
    cfg = _alembic_config()
    script = ScriptDirectory.from_config(cfg)
    head = script.get_current_head()
    current = _current_revision(engine)

    pending: list[str] = []
    if current != head and head is not None:
        # Walk from head back to current; reverse to get apply order.
        # When current is None (fresh DB) we list every revision.
        if current is None:
            pending = [rev.revision for rev in script.walk_revisions()][::-1]
        else:
            for rev in script.walk_revisions(head, current):
                if rev.revision != current:
                    pending.append(rev.revision)
            pending.reverse()

    return {
        "current_revision": current,
        "head_revision": head,
        "is_up_to_date": current == head and head is not None,
        "pending_migrations": pending,
        "legacy_create_all": _has_legacy_tables(engine),
    }


def apply_migrations(engine: Engine) -> dict[str, Any]:
    """Drive the full migration flow at startup.

    Order of operations:

      1. If the DB has app tables but no alembic_version, stamp head.
         This is the bridge for v0.1.x deployments — without it the
         next upgrade tries to ``CREATE TABLE`` over existing tables
         and fails. Logged loudly so operators see the bridge fire.

      2. Run ``alembic upgrade head`` unconditionally. This is a
         no-op when current == head (the common steady-state case).

      3. Return a status dict (same shape as ``schema_status``) so
         callers can log what happened.

    Raises ``MigrationError`` on any failure. Callers should crash
    the process — a partially-migrated schema is worse than not
    starting at all.
    """
    cfg = _alembic_config()
    head = _current_head(cfg)
    legacy = _has_legacy_tables(engine)

    if legacy:
        logger.warning(
            "Database has application tables but no alembic_version row — "
            "treating as a legacy v0.1.x create_all deployment and stamping "
            "at head=%s. Future schema changes will go through Alembic.",
            head,
        )
        try:
            command.stamp(cfg, head)
        except Exception as e:
            raise MigrationError(
                f"Failed to stamp legacy database at head={head}: {e}"
            ) from e

    try:
        command.upgrade(cfg, "head")
    except Exception as e:
        raise MigrationError(f"alembic upgrade head failed: {e}") from e

    status = schema_status(engine)
    if status["is_up_to_date"]:
        logger.info(
            "Database migrations applied successfully (revision=%s)",
            status["current_revision"],
        )
    else:
        # Shouldn't happen — upgrade head reported success but status
        # disagrees. Surface this rather than ignore.
        raise MigrationError(
            f"Migration mismatch after upgrade: current={status['current_revision']!r}, "
            f"head={status['head_revision']!r}"
        )
    return status
