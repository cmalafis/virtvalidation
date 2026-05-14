"""Tests for the Alembic migration story.

Covers the three things that have to keep working forever:

  1. Every migration applies cleanly to an empty DB.
  2. Every migration rolls back cleanly.
  3. The schema implied by the models matches the schema the
     migrations produce — i.e. nobody added a model field without
     also adding a migration.

Plus the bridge logic: a DB with application tables but no
``alembic_version`` row gets stamped at head before upgrade so
v0.1.x deployments survive the cutover.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.core.db import Base
from app.core.migrations import (
    MigrationError,
    apply_migrations,
    schema_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _fresh_sqlite_url() -> tuple[str, str]:
    """Make an isolated SQLite file for one test run.

    Returns ``(url, path)`` so the caller can clean up. We use a
    file-backed DB rather than ``:memory:`` because Alembic opens
    its own connections via the URL — an in-memory DB would land in
    a different connection from the one the test inspects.
    """
    fd, path = tempfile.mkstemp(suffix=".db", prefix="alembic_test_")
    os.close(fd)
    return f"sqlite:///{path}", path


def _alembic_config(url: str) -> Config:
    """Build an Alembic config rooted at ``backend/alembic.ini`` with
    a per-test SQLite URL injected."""
    repo_root = Path(__file__).resolve().parents[1]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def temp_db():
    """Yield an isolated SQLite file URL; clean it up afterwards."""
    url, path = _fresh_sqlite_url()
    try:
        yield url
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# 1. Forward apply
# ---------------------------------------------------------------------------
def test_upgrade_head_applies_cleanly_against_empty_db(temp_db):
    cfg = _alembic_config(temp_db)
    command.upgrade(cfg, "head")

    engine = create_engine(temp_db)
    tables = set(inspect(engine).get_table_names())

    # Spot-check every model the app needs. If any of these are missing
    # after a clean upgrade, the baseline migration regressed.
    expected = {
        "vms",
        "baseline_snapshots",
        "validation_results",
        "migration_plans",
        "audit_logs",
        "app_settings",
        "network_design_reviews",
        "network_findings",
        "vcenter_sources",
        "vm_groups",
        "vm_group_members",
        "migration_programs",
        "alembic_version",
    }
    missing = expected - tables
    assert not missing, f"Migration head missing tables: {sorted(missing)}"


def test_upgrade_head_records_revision_in_alembic_version(temp_db):
    cfg = _alembic_config(temp_db)
    command.upgrade(cfg, "head")

    engine = create_engine(temp_db)
    status = schema_status(engine)
    assert status["is_up_to_date"] is True
    assert status["current_revision"] is not None
    assert status["current_revision"] == status["head_revision"]
    assert status["pending_migrations"] == []


# ---------------------------------------------------------------------------
# 2. Reversible
# ---------------------------------------------------------------------------
def test_downgrade_to_base_drops_every_application_table(temp_db):
    cfg = _alembic_config(temp_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")

    engine = create_engine(temp_db)
    tables = set(inspect(engine).get_table_names())

    # alembic_version may stick around at base — that's fine. What
    # matters is that no application tables survive a full downgrade.
    leftovers = tables - {"alembic_version"}
    assert leftovers == set(), f"Downgrade left tables behind: {sorted(leftovers)}"


def test_upgrade_downgrade_upgrade_round_trips_cleanly(temp_db):
    """Idempotency check — the same migration applied twice must
    produce the same end state without errors."""
    cfg = _alembic_config(temp_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(temp_db)
    assert schema_status(engine)["is_up_to_date"] is True


# ---------------------------------------------------------------------------
# 3. Models match migrations (drift check)
# ---------------------------------------------------------------------------
def test_models_match_migrations_no_drift(temp_db):
    """If a model field was added without a corresponding migration,
    the schema produced by ``alembic upgrade head`` will differ from
    the schema ``Base.metadata.create_all`` would produce. Catch
    that delta here so the bug doesn't reach production again."""
    # Apply migrations.
    cfg = _alembic_config(temp_db)
    command.upgrade(cfg, "head")
    migrated_engine = create_engine(temp_db)
    migrated_tables = set(inspect(migrated_engine).get_table_names())

    # Build a parallel DB from the live model metadata.
    fresh_url, fresh_path = _fresh_sqlite_url()
    try:
        fresh_engine = create_engine(fresh_url)
        Base.metadata.create_all(bind=fresh_engine)
        fresh_tables = set(inspect(fresh_engine).get_table_names())

        # Drop the alembic_version table from the migrated set since
        # the create_all path doesn't produce it.
        migrated_app_tables = migrated_tables - {"alembic_version"}
        fresh_app_tables = fresh_tables

        only_in_migrations = migrated_app_tables - fresh_app_tables
        only_in_models = fresh_app_tables - migrated_app_tables
        assert only_in_migrations == set(), (
            f"Tables in migrations but not in models: {sorted(only_in_migrations)} "
            "— either remove the orphaned migration or restore the model."
        )
        assert only_in_models == set(), (
            f"Tables in models but not in migrations: {sorted(only_in_models)} "
            '— run `alembic revision --autogenerate -m "..."` to capture them.'
        )

        # Per-table column drift. We compare column names + nullability;
        # type comparison across SQLAlchemy/Alembic dialects is fragile
        # and over-flags non-issues like VARCHAR vs TEXT.
        migrated_inspector = inspect(migrated_engine)
        fresh_inspector = inspect(fresh_engine)
        for table in sorted(migrated_app_tables):
            migrated_cols = {
                c["name"]: c.get("nullable") for c in migrated_inspector.get_columns(table)
            }
            fresh_cols = {c["name"]: c.get("nullable") for c in fresh_inspector.get_columns(table)}
            assert migrated_cols == fresh_cols, (
                f"Column drift on table {table!r}:\n"
                f"  migrations: {migrated_cols}\n"
                f"  models:     {fresh_cols}\n"
                'Run `alembic revision --autogenerate -m "..."` to capture the diff.'
            )
    finally:
        try:
            os.unlink(fresh_path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# 4. Bridge for v0.1.x create_all deployments
# ---------------------------------------------------------------------------
def test_apply_migrations_stamps_legacy_create_all_databases(temp_db):
    """The bridge: a DB with app tables but no alembic_version is
    stamped at head before upgrade runs, so v0.1.x deployments
    don't crash on the first post-cutover restart."""
    # Simulate a legacy DB by using create_all directly — exactly
    # what the v0.1.x lifespan would have produced.
    legacy_engine = create_engine(temp_db)
    Base.metadata.create_all(bind=legacy_engine)
    assert "alembic_version" not in inspect(legacy_engine).get_table_names()

    # apply_migrations reads alembic.ini relative to its own module
    # location, so we need to point the env.py at our test DB.
    monkey_url = os.environ.pop("ALEMBIC_DATABASE_URL", None)
    os.environ["ALEMBIC_DATABASE_URL"] = temp_db
    try:
        status = apply_migrations(legacy_engine)
    finally:
        if monkey_url is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = monkey_url

    assert status["is_up_to_date"] is True
    # alembic_version must now exist with the stamped revision.
    assert "alembic_version" in inspect(legacy_engine).get_table_names()


def test_apply_migrations_is_noop_on_already_migrated_db(temp_db):
    """The steady-state case — DB is already at head. apply_migrations
    should run cleanly and report up-to-date without changes."""
    # First-time apply.
    os.environ["ALEMBIC_DATABASE_URL"] = temp_db
    try:
        engine = create_engine(temp_db)
        first = apply_migrations(engine)
        assert first["is_up_to_date"] is True
        # Second invocation — no-op.
        second = apply_migrations(engine)
        assert second["is_up_to_date"] is True
        assert second["current_revision"] == first["current_revision"]
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)


# ---------------------------------------------------------------------------
# 5. Schema status reporting
# ---------------------------------------------------------------------------
def test_schema_status_reports_pending_when_not_upgraded(temp_db):
    """A fresh, unmigrated DB should report ``current=None`` and
    every migration as pending."""
    engine = create_engine(temp_db)
    os.environ["ALEMBIC_DATABASE_URL"] = temp_db
    try:
        status = schema_status(engine)
    finally:
        os.environ.pop("ALEMBIC_DATABASE_URL", None)

    assert status["current_revision"] is None
    assert status["head_revision"] is not None
    assert status["is_up_to_date"] is False
    assert len(status["pending_migrations"]) >= 1


def test_schema_status_endpoint_returns_payload(client):
    """GET /api/health/schema returns the full status dict."""
    r = client.get("/api/health/schema")
    assert r.status_code == 200
    body = r.json()
    assert "current_revision" in body
    assert "head_revision" in body
    assert "is_up_to_date" in body
    assert "pending_migrations" in body
    assert "legacy_create_all" in body


# ---------------------------------------------------------------------------
# 6. Migration script integrity
# ---------------------------------------------------------------------------
def test_every_migration_has_upgrade_and_downgrade():
    """Every migration script must define both ``upgrade()`` and
    ``downgrade()``. Missing downgrades are how teams end up with
    one-way migrations they can't roll back from in production."""
    cfg = _alembic_config("sqlite:///dummy")
    script = ScriptDirectory.from_config(cfg)

    revisions = list(script.walk_revisions())
    assert revisions, "No migrations found in versions/ — at least the baseline must exist."

    for rev in revisions:
        # rev.path is the absolute path emitted by the Alembic script
        # directory; no need to resolve relative to repo_root.
        actual_path = Path(rev.path)
        text = actual_path.read_text()
        assert "def upgrade()" in text, f"{actual_path} missing upgrade()"
        assert "def downgrade()" in text, f"{actual_path} missing downgrade()"


def test_baseline_migration_has_no_predecessor():
    """The baseline migration's ``down_revision`` must be ``None`` —
    if a future migration accidentally rebuilds the baseline as a
    successor, the chain breaks for new deployments."""
    cfg = _alembic_config("sqlite:///dummy")
    script = ScriptDirectory.from_config(cfg)
    bases = list(script.get_bases())
    assert len(bases) == 1, f"Expected exactly one base revision, got {bases}"


def test_migration_error_raised_when_alembic_ini_missing(monkeypatch, tmp_path):
    """If alembic.ini disappears, apply_migrations must fail loud
    rather than silently skip migrations."""
    from app.core import migrations as migrations_module

    # Point the resolver at a path that has no alembic.ini.
    monkeypatch.setattr(
        migrations_module,
        "_alembic_config",
        lambda: (_ for _ in ()).throw(MigrationError("alembic.ini not found")),
    )
    engine = create_engine("sqlite:///:memory:")
    with pytest.raises(MigrationError, match="not found"):
        apply_migrations(engine)
