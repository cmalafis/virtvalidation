"""Tests for the deterministic collection engine.

These tests mock paramiko so the engine can be exercised without a real
SSH target. The key invariants pinned here:

  - Successful collection produces a populated ``collected_data`` + a
    host key fingerprint.
  - Each failure mode maps to its canonical category string.
  - Per-VM failures NEVER raise out of ``CollectionEngine.collect``.
  - Missing probe data becomes ``partial`` (succeeded=True) rather than
    a hard failure — the operator gets the partial baseline + a flag.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import paramiko
import pytest

from app.core.collection.collector_spec import (
    PASS1_CATALOG_VERSION,
    pass1_data_keys,
    pass1_probe_names,
)
from app.core.collection.engine import (
    FAILURE_CATEGORIES,
    CollectionEngine,
    VMTarget,
    _categorize_ssh_error,
)
from app.core.ssh import SSHCollectionError


@pytest.fixture
def fake_key():
    return MagicMock(spec=paramiko.PKey)


def _full_state() -> dict:
    """A state dict that satisfies every Pass-1 ``available`` probe."""
    keys = pass1_data_keys()
    return {
        "meta": {"host": "h", "hostname": "h.corp", "kernel": "5.14", "os": {}},
        "services": [],
        "network": {"interfaces": {}, "routes": [], "dns": []},
        "ports": [],
        "mounts": [],
        "cron": {"user_crontabs": {}, "system": []},
        **{k: {} for k in keys - {"meta", "services", "network", "ports", "mounts", "cron"}},
    }


def _make_engine_with_collect(fake_key, monkeypatch, *, side_effect):
    """Build an engine and patch the collector that the engine produces.

    ``side_effect`` is invoked when ``SSHCollector.collect`` is called —
    can be a callable, a return value, or an exception class/instance.
    """
    engine = CollectionEngine(paramiko_key=fake_key)

    fake_collector = MagicMock()
    fake_collector.last_host_key_event = None
    if isinstance(side_effect, Exception):
        fake_collector.collect.side_effect = side_effect
    elif callable(side_effect) and not isinstance(side_effect, type):
        fake_collector.collect.side_effect = side_effect
    else:
        fake_collector.collect.return_value = side_effect

    monkeypatch.setattr(engine, "_make_collector", lambda: fake_collector)
    return engine, fake_collector


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
def test_collect_returns_succeeded_result_with_collected_data(fake_key, monkeypatch):
    state = _full_state()
    engine, collector = _make_engine_with_collect(fake_key, monkeypatch, side_effect=state)
    collector.last_host_key_event = {
        "action": "added",
        "host": "10.0.0.5",
        "key_type": "ssh-ed25519",
        "fingerprint": "SHA256:abc",
    }

    result = engine.collect(VMTarget(vm_id=1, host="10.0.0.5"))

    assert result.succeeded is True
    assert result.vm_id == 1
    assert result.collected_data == state
    assert result.host_key_fingerprint == "SHA256:abc"
    assert result.failure_category is None
    assert result.probe_catalog_version == PASS1_CATALOG_VERSION
    assert set(result.probes_run) == set(pass1_probe_names())
    assert result.duration_ms >= 0
    assert result.completed_at is not None


def test_collect_returns_partial_when_probes_missing(fake_key, monkeypatch):
    """A partial state dict produces ``succeeded=True`` + category=partial."""
    state = {
        "meta": {},
        "services": [],
        # network/ports/mounts/cron deliberately absent
    }
    engine, _ = _make_engine_with_collect(fake_key, monkeypatch, side_effect=state)

    result = engine.collect(VMTarget(vm_id=2, host="10.0.0.6"))

    assert result.succeeded is True
    assert result.failure_category == "partial"
    assert "missing probe data" in (result.failure_detail or "")
    assert result.collected_data == state
    # probes_run reflects what actually returned data, not the full catalog.
    assert "vm_identity" in result.probes_run
    assert "systemd_services" in result.probes_run
    assert "network_interfaces" not in result.probes_run


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------
def test_missing_host_returns_unreachable(fake_key):
    engine = CollectionEngine(paramiko_key=fake_key)
    result = engine.collect(VMTarget(vm_id=3, host=""))
    assert result.succeeded is False
    assert result.failure_category == "unreachable"
    assert "no resolvable host" in (result.failure_detail or "")


def test_host_key_mismatch_is_categorized(fake_key, monkeypatch):
    err = SSHCollectionError(
        "Host key verification failed for h",
        kind="host_key_mismatch",
        host="h",
        fingerprint="SHA256:newkey",
    )
    engine, _ = _make_engine_with_collect(fake_key, monkeypatch, side_effect=err)
    result = engine.collect(VMTarget(vm_id=4, host="h"))
    assert result.succeeded is False
    assert result.failure_category == "host_key_changed"
    assert result.host_key_fingerprint == "SHA256:newkey"


def test_auth_failure_is_categorized(fake_key, monkeypatch):
    err = paramiko.AuthenticationException("bad key")
    # Engine's collector call raises bare paramiko exception (uncommon —
    # SSHCollector wraps most — but the engine must still categorize it).
    engine, _ = _make_engine_with_collect(fake_key, monkeypatch, side_effect=err)
    result = engine.collect(VMTarget(vm_id=5, host="h"))
    assert result.succeeded is False
    assert result.failure_category == "auth_failed"


def test_generic_ssh_error_with_timeout_message_is_timeout(fake_key, monkeypatch):
    err = SSHCollectionError("connection timed out after 10s", host="h")
    engine, _ = _make_engine_with_collect(fake_key, monkeypatch, side_effect=err)
    result = engine.collect(VMTarget(vm_id=6, host="h"))
    assert result.failure_category == "timeout"


def test_generic_ssh_error_with_unreachable_message_is_unreachable(fake_key, monkeypatch):
    err = SSHCollectionError("Connection refused", host="h")
    engine, _ = _make_engine_with_collect(fake_key, monkeypatch, side_effect=err)
    result = engine.collect(VMTarget(vm_id=7, host="h"))
    assert result.failure_category == "unreachable"


def test_unexpected_exception_becomes_command_failed_not_raise(fake_key, monkeypatch):
    """If anything inside the collector raises an arbitrary exception, the
    engine must convert it to a failed result — never let it propagate."""

    def boom(*args, **kwargs):
        raise RuntimeError("totally unexpected")

    engine, _ = _make_engine_with_collect(fake_key, monkeypatch, side_effect=boom)
    result = engine.collect(VMTarget(vm_id=8, host="h"))
    assert result.succeeded is False
    assert result.failure_category == "command_failed"
    assert "RuntimeError" in (result.failure_detail or "")


def test_every_failure_category_is_in_canonical_set():
    """Every failure category the engine emits must be one of the documented
    strings — drift here means the frontend filter UI silently breaks."""
    for category in FAILURE_CATEGORIES:
        # Trivial map check — keeps the constant pinned.
        assert isinstance(category, str)
    assert "unreachable" in FAILURE_CATEGORIES
    assert "auth_failed" in FAILURE_CATEGORIES
    assert "host_key_changed" in FAILURE_CATEGORIES
    assert "command_failed" in FAILURE_CATEGORIES
    assert "partial" in FAILURE_CATEGORIES
    assert "timeout" in FAILURE_CATEGORIES


# ---------------------------------------------------------------------------
# _categorize_ssh_error specifics
# ---------------------------------------------------------------------------
def test_categorize_host_key_unknown_maps_to_auth_failed():
    err = SSHCollectionError("strict; not in known_hosts", kind="host_key_unknown")
    assert _categorize_ssh_error(err) == "auth_failed"


def test_categorize_name_resolution_message_maps_to_unreachable():
    err = SSHCollectionError("Name or service not known: foo", host="foo")
    assert _categorize_ssh_error(err) == "unreachable"


# ---------------------------------------------------------------------------
# Probe accounting
# ---------------------------------------------------------------------------
def test_pass1_probe_names_default_returns_only_available():
    names = pass1_probe_names()
    # Available-only includes the existing SSHCollector subsystems.
    assert "vm_identity" in names
    assert "systemd_services" in names
    assert "listening_ports" in names
    # Probes declared but not yet wired in are excluded by default.
    assert "uptime_load" not in names
    assert "memory" not in names


def test_pass1_probe_names_full_includes_future_probes():
    full = pass1_probe_names(available_only=False)
    assert "uptime_load" in full
    assert "memory" in full
    assert "tls_cert_expiry" in full
