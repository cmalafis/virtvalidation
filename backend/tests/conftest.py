# ruff: noqa: E402, I001
"""
Shared pytest fixtures.

DATABASE_URL and OLLAMA_HOST are set BEFORE importing the app so that
`app.core.config.Settings()` resolves to SQLite + a dummy LLM endpoint.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("OLLAMA_HOST", "http://localhost:0")
os.environ.setdefault("OLLAMA_MODEL", "test-model")
# Resolve CSV template against the repo root so tests work regardless of
# whether pytest is invoked from backend/ or the repo root.
os.environ.setdefault(
    "CSV_TEMPLATE_PATH",
    str(Path(__file__).resolve().parents[2] / "docs" / "vm-inventory-template.csv"),
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.db import Base, get_db
from app.main import app


@pytest.fixture
def engine():
    """Fresh in-memory SQLite engine with all tables created."""
    eng = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    try:
        yield eng
    finally:
        Base.metadata.drop_all(bind=eng)
        eng.dispose()


@pytest.fixture
def db_session(engine):
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client(engine):
    """
    TestClient with get_db overridden to use the test engine.

    Intentionally NOT used as a context manager — that would trigger the
    FastAPI lifespan (which starts the APScheduler job and targets the
    production engine). Dependency overrides give us a clean DB per test
    without standing up the scheduler.

    SessionLocal is rebound at the module level so middleware that opens its
    own session (e.g. AuditMiddleware) hits the same test database.
    """
    from app.core import db as _db

    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def _get_db():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    original_sessionlocal = _db.SessionLocal
    _db.SessionLocal = Session
    app.dependency_overrides[get_db] = _get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        _db.SessionLocal = original_sessionlocal


# ---------- shared data fixtures ----------


@pytest.fixture
def mock_vm_payload():
    return {
        "name": "db-01",
        "source_hostname": "db-01.vmware.local",
        "ip_address": "10.0.0.5",
        "os_family": "rhel",
        "role": "database",
    }


@pytest.fixture
def mock_ssh_state():
    """Shape returned by SSHCollector.collect()."""
    return {
        "meta": {
            "host": "10.0.0.5",
            "username": "virtvalidate",
            "hostname": "db-01.corp",
            "os": {"id": "rhel", "version_id": "9.2", "pretty_name": "RHEL 9.2"},
            "kernel": "5.14.0-362",
            "os_profile": {
                "distro": "rhel",
                "distro_family": "rhel-like",
                "major_version": 9,
                "minor_version": 2,
                "kernel_version": "5.14.0-362",
                "architecture": "x86_64",
                "is_systemd": True,
                "pretty_name": "RHEL 9.2",
                "detection_confidence": "high",
            },
            "collected_at": "2026-04-23T12:00:00+00:00",
        },
        "services": [
            {
                "unit": "sshd.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "OpenSSH server daemon",
            },
            {
                "unit": "postgresql.service",
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "description": "PostgreSQL 16",
            },
        ],
        "network": {
            "interfaces": {"eth0": {"ipv4": ["10.0.0.5/24"], "ipv6": []}},
            "routes": ["default via 10.0.0.1 dev eth0"],
            "dns": ["8.8.8.8"],
        },
        "ports": [
            {"proto": "tcp", "state": "LISTEN", "address": "0.0.0.0", "port": 22},
            {"proto": "tcp", "state": "LISTEN", "address": "0.0.0.0", "port": 5432},
        ],
        "mounts": [{"target": "/", "source": "/dev/sda1", "fstype": "xfs", "options": "rw"}],
        "cron": {"user_crontabs": {}, "system": []},
    }


@pytest.fixture
def mock_ollama_verdict():
    return {
        "status": "pass",
        "summary": "VM healthy after migration",
        "findings": [],
        "remediation": [],
    }


@pytest.fixture
def mock_ollama_plan():
    return {
        "summary": "DB first, then app tier.",
        "waves": [
            {
                "wave_number": 1,
                "vm_ids": [1],
                "rationale": "Stateful DB migrates before dependent apps",
                "estimated_risk": "high",
            },
            {
                "wave_number": 2,
                "vm_ids": [2, 3],
                "rationale": "Stateless app servers follow the DB",
                "estimated_risk": "medium",
            },
        ],
    }
