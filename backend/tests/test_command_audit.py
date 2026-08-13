"""Tests for the per-command SSH audit trail.

Covers the SSHCollector accumulator (one record per command, blocked-command
capture) and the read-only API surface.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from app.core import db as _db_module
from app.core.ssh import SSHCollector
from app.core.ssh_guard import SSHCommandNotAllowed
from app.models.command_audit import CommandAudit


class _StubChannel:
    def __init__(self, exit_code: int = 0):
        self._exit_code = exit_code

    def recv_exit_status(self) -> int:
        return self._exit_code


class _StubStdout:
    def __init__(self, raw: bytes, exit_code: int = 0):
        self._raw = raw
        self.channel = _StubChannel(exit_code)

    def read(self) -> bytes:
        return self._raw


# ---------------------------------------------------------------------------
# Collector accumulator
# ---------------------------------------------------------------------------
def test_run_records_command_with_hash_and_duration():
    collector = SSHCollector(key_path="/dev/null")

    class _Client:
        def exec_command(self, cmd, timeout=0):
            return None, _StubStdout(b"hello\n"), _StubStdout(b"")

    out = collector._run(_Client(), "uname -r")
    assert out == "hello\n"
    assert len(collector.command_log) == 1
    rec = collector.command_log[0]
    assert rec["command"] == "uname -r"
    assert rec["exit_status"] == 0
    assert rec["stdout_byte_count"] == 6
    assert rec["stdout_sha256"] is not None and len(rec["stdout_sha256"]) == 64
    assert rec["stdout_truncated"] == "hello\n"
    assert rec["blocked"] is False
    assert rec["duration_ms"] >= 0


def test_run_records_nonzero_exit_command():
    collector = SSHCollector(key_path="/dev/null")

    class _Client:
        def exec_command(self, cmd, timeout=0):
            return None, _StubStdout(b"", exit_code=1), _StubStdout(b"")

    out = collector._run(_Client(), "ss -tulnH")
    assert out == ""  # non-zero exit returns empty per existing contract
    assert len(collector.command_log) == 1
    assert collector.command_log[0]["exit_status"] == 1


def test_run_records_blocked_command_then_raises():
    collector = SSHCollector(key_path="/dev/null")

    class _Client:
        def exec_command(self, cmd, timeout=0):  # pragma: no cover — must not run
            raise AssertionError("blocked command must never reach exec_command")

    with pytest.raises(SSHCommandNotAllowed):
        collector._run(_Client(), "rm -rf /var/log")
    assert len(collector.command_log) == 1
    rec = collector.command_log[0]
    assert rec["blocked"] is True
    assert rec["exit_status"] is None
    assert rec["command"] == "rm -rf /var/log"


def test_collect_resets_command_log_per_vm(monkeypatch):
    """Each collect() starts a fresh command_log — no cross-VM bleed."""
    from tests.test_os_profile import OS_RELEASE_RHEL_9, _exec, _StubClient

    responses = {
        "cat /etc/os-release": OS_RELEASE_RHEL_9,
        "uname -r": "5.14.0\n",
        "uname -m": "x86_64\n",
        "hostname -f || hostname": "db.corp\n",
    }
    stub = _StubClient(responses)
    stub.exec_command = lambda cmd, timeout=0: _exec(stub, cmd, timeout)

    @contextmanager
    def fake_connect(self, host, username):
        yield stub

    monkeypatch.setattr(SSHCollector, "_connect", fake_connect)
    collector = SSHCollector(key_path="/dev/null")
    collector.collect(host="10.0.0.5")
    first_count = len(collector.command_log)
    assert first_count > 0
    # Second collection must not accumulate onto the first.
    collector.collect(host="10.0.0.6")
    assert len(collector.command_log) > 0
    assert len(collector.command_log) <= first_count + 2  # same probe set, not doubled


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
@pytest.fixture
def seed_command_audits(client):
    session = _db_module.SessionLocal()
    try:
        session.add_all(
            [
                CommandAudit(
                    vm_id=1,
                    host="10.0.0.5",
                    run_type="baseline",
                    run_id=10,
                    command="uname -r",
                    exit_status=0,
                    stdout_byte_count=6,
                    stdout_sha256="a" * 64,
                    duration_ms=5,
                    blocked=False,
                ),
                CommandAudit(
                    vm_id=1,
                    host="10.0.0.5",
                    run_type="baseline",
                    run_id=10,
                    command="ip -o -4 addr show",
                    exit_status=0,
                    blocked=False,
                ),
                CommandAudit(
                    vm_id=2,
                    host="10.0.0.6",
                    run_type="validation",
                    run_id=20,
                    command="rm -rf /",
                    exit_status=None,
                    blocked=True,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()
    return client


def test_list_command_audits_pagination_shape(seed_command_audits):
    resp = seed_command_audits.get("/api/command-audits")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"items", "total", "skip", "limit"}
    assert body["total"] == 3


def test_list_command_audits_filters(seed_command_audits):
    assert (
        seed_command_audits.get("/api/command-audits", params={"run_type": "baseline"}).json()[
            "total"
        ]
        == 2
    )
    assert (
        seed_command_audits.get("/api/command-audits", params={"run_id": 20}).json()["total"] == 1
    )
    body = seed_command_audits.get("/api/command-audits", params={"blocked": True}).json()
    assert body["total"] == 1
    assert body["items"][0]["command"] == "rm -rf /"
    assert body["items"][0]["blocked"] is True
