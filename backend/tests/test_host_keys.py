"""Tests for SSH host-key handling: TOFU, persistence, mismatch, strict mode.

Mocks paramiko's ``SSHClient.connect`` so we can simulate first-time
acceptance, mismatches, and strict-mode rejections without spinning up
an SSH server.
"""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.core.ssh import (
    SSHCollectionError,
    SSHCollector,
    _fingerprint_for,
    _RecordingAutoAddPolicy,
)


def _make_ed25519_key() -> paramiko.Ed25519Key:
    """paramiko.Ed25519Key has no .generate() — cut a fresh one through
    cryptography and feed it to paramiko via an in-memory file."""
    raw = Ed25519PrivateKey.generate()
    pem = raw.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return paramiko.Ed25519Key.from_private_key(io.StringIO(pem.decode("ascii")))


@pytest.fixture
def fake_key_files(tmp_path: Path) -> dict[str, Path]:
    """Real Ed25519 private key on disk so SSHCollector's
    ``Ed25519Key.from_private_key_file`` succeeds. Returns paths to the
    private key + a not-yet-existing known_hosts location."""
    raw = Ed25519PrivateKey.generate()
    pem = raw.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "id_ed25519"
    key_path.write_bytes(pem)
    return {
        "key_path": key_path,
        "known_hosts": tmp_path / "known_hosts",
    }


def _make_collector(fake_key_files, *, policy: str = "auto_accept") -> SSHCollector:
    return SSHCollector(
        key_path=str(fake_key_files["key_path"]),
        known_hosts_path=str(fake_key_files["known_hosts"]),
        host_key_policy=policy,
    )


@pytest.fixture(autouse=True)
def _silence_paramiko_log(monkeypatch):
    """paramiko's policies call ``client._log(...)`` which dereferences
    ``self._transport``. Our fake clients never go through .connect(), so
    transport is None and the inner log machinery explodes. Stub it."""
    monkeypatch.setattr(paramiko.SSHClient, "_log", lambda self, level, msg: None)


def _patch_connect_to_invoke_policy(server_key: paramiko.PKey, *, hostname: str = "10.0.0.5"):
    """Build a side_effect for SSHClient.connect that simulates paramiko's
    real flow: it consults the configured missing_host_key_policy when
    the host isn't in known_hosts, raises BadHostKeyException when it is
    but the keys differ, and returns silently when they match."""

    def _side_effect(self, hostname=hostname, **kwargs):
        host_keys = self._host_keys
        if hostname in host_keys.lookup(hostname).__class__.__bases__ if False else False:
            pass
        try:
            stored = host_keys.lookup(hostname)
        except Exception:
            stored = None

        if stored is None:
            # No stored entry — defer to whatever policy is set.
            self._policy.missing_host_key(self, hostname, server_key)
            # AutoAddPolicy adds it; RejectPolicy raises.
        else:
            # Compare key types stored against what the server presented.
            existing = stored.get(server_key.get_name())
            if existing is None or existing.asbytes() != server_key.asbytes():
                raise paramiko.BadHostKeyException(hostname, server_key, existing or server_key)
            # else: key matches — silent success.

        # Stash the server key so get_remote_server_key works for the
        # "verified" path's fingerprint capture.
        self._fake_server_key = server_key

    return _side_effect


def _patch_get_transport(server_key: paramiko.PKey):
    """Make get_transport().get_remote_server_key() return the simulated key."""

    def _patched(self):
        m = MagicMock()
        m.get_remote_server_key = lambda: getattr(self, "_fake_server_key", server_key)
        return m

    return _patched


# ---------------------------------------------------------------------------
# fingerprint helper
# ---------------------------------------------------------------------------
def test_fingerprint_format_matches_openssh():
    key = _make_ed25519_key()
    fp = _fingerprint_for(key)
    assert fp.startswith("SHA256:")
    # base64 portion has no trailing `=` padding (matches `ssh-keygen -l`).
    assert "=" not in fp.split(":", 1)[1]


# ---------------------------------------------------------------------------
# AutoAdd policy records every accepted key
# ---------------------------------------------------------------------------
def test_recording_policy_captures_added_key():
    sink: list[dict] = []
    policy = _RecordingAutoAddPolicy(sink)
    server_key = _make_ed25519_key()
    fake_client = MagicMock()
    fake_client._host_keys = paramiko.HostKeys()

    policy.missing_host_key(fake_client, "10.0.0.5", server_key)
    assert len(sink) == 1
    assert sink[0]["host"] == "10.0.0.5"
    assert sink[0]["key_type"] == "ssh-ed25519"
    assert sink[0]["fingerprint"].startswith("SHA256:")
    assert sink[0]["action"] == "added"


# ---------------------------------------------------------------------------
# First connection auto-accepts and persists the host key
# ---------------------------------------------------------------------------
def test_first_connection_adds_and_persists_host_key(fake_key_files, monkeypatch):
    server_key = _make_ed25519_key()
    collector = _make_collector(fake_key_files, policy="auto_accept")

    monkeypatch.setattr(paramiko.SSHClient, "connect", _patch_connect_to_invoke_policy(server_key))
    monkeypatch.setattr(paramiko.SSHClient, "get_transport", _patch_get_transport(server_key))

    with collector._connect("10.0.0.5", "virtvalidate"):
        pass

    # Event marker captures the TOFU acceptance.
    event = collector.last_host_key_event
    assert event is not None
    assert event["action"] == "added"
    assert event["host"] == "10.0.0.5"
    assert event["fingerprint"].startswith("SHA256:")
    # known_hosts was written to disk for the next connection.
    assert fake_key_files["known_hosts"].is_file()
    text = fake_key_files["known_hosts"].read_text()
    assert "ssh-ed25519" in text


# ---------------------------------------------------------------------------
# Subsequent connection verifies the stored key (no audit-worthy event)
# ---------------------------------------------------------------------------
def test_subsequent_connection_verifies_stored_key(fake_key_files, monkeypatch):
    server_key = _make_ed25519_key()
    # Pre-populate known_hosts with the same key.
    host_keys = paramiko.HostKeys()
    host_keys.add("10.0.0.5", server_key.get_name(), server_key)
    host_keys.save(str(fake_key_files["known_hosts"]))

    collector = _make_collector(fake_key_files, policy="auto_accept")
    monkeypatch.setattr(paramiko.SSHClient, "connect", _patch_connect_to_invoke_policy(server_key))
    monkeypatch.setattr(paramiko.SSHClient, "get_transport", _patch_get_transport(server_key))

    with collector._connect("10.0.0.5", "virtvalidate"):
        pass

    event = collector.last_host_key_event
    assert event is not None
    # "verified" — never lands in the audit trail (would be too noisy).
    assert event["action"] == "verified"
    assert event["host"] == "10.0.0.5"


# ---------------------------------------------------------------------------
# Mismatched stored key raises with the security-event message
# ---------------------------------------------------------------------------
def test_mismatched_host_key_raises_with_clear_message(fake_key_files, monkeypatch):
    old_key = _make_ed25519_key()
    new_key = _make_ed25519_key()
    # Pre-populate known_hosts with the OLD key.
    host_keys = paramiko.HostKeys()
    host_keys.add("10.0.0.5", old_key.get_name(), old_key)
    host_keys.save(str(fake_key_files["known_hosts"]))

    collector = _make_collector(fake_key_files, policy="auto_accept")
    # Server now presents the NEW key — should explode with a mismatch.
    monkeypatch.setattr(paramiko.SSHClient, "connect", _patch_connect_to_invoke_policy(new_key))

    with pytest.raises(SSHCollectionError) as exc:
        with collector._connect("10.0.0.5", "virtvalidate"):
            pass

    err = exc.value
    assert err.kind == "host_key_mismatch"
    assert err.host == "10.0.0.5"
    assert err.fingerprint.startswith("SHA256:")
    msg = str(err)
    assert "man-in-the-middle" in msg
    assert str(fake_key_files["known_hosts"]) in msg
    assert err.fingerprint in msg


# ---------------------------------------------------------------------------
# Strict mode rejects unknown hosts with actionable remediation
# ---------------------------------------------------------------------------
def test_strict_mode_rejects_unknown_host(fake_key_files, monkeypatch):
    server_key = _make_ed25519_key()
    collector = _make_collector(fake_key_files, policy="strict")

    # paramiko's RejectPolicy raises SSHException("Server '<host>' not
    # found in known_hosts") — simulate that exact path.
    def _strict_connect(self, hostname, **kwargs):
        host_keys = self._host_keys
        try:
            stored = host_keys.lookup(hostname)
        except Exception:
            stored = None
        if stored is None:
            self._policy.missing_host_key(self, hostname, server_key)

    monkeypatch.setattr(paramiko.SSHClient, "connect", _strict_connect)

    with pytest.raises(SSHCollectionError) as exc:
        with collector._connect("10.0.0.5", "virtvalidate"):
            pass

    err = exc.value
    assert err.kind == "host_key_unknown"
    assert err.host == "10.0.0.5"
    msg = str(err)
    assert "Strict host-key checking is enabled" in msg
    assert "ssh-keyscan" in msg
    assert str(fake_key_files["known_hosts"]) in msg


# ---------------------------------------------------------------------------
# Strict mode allows a previously-known host
# ---------------------------------------------------------------------------
def test_strict_mode_allows_known_host(fake_key_files, monkeypatch):
    server_key = _make_ed25519_key()
    host_keys = paramiko.HostKeys()
    host_keys.add("10.0.0.5", server_key.get_name(), server_key)
    host_keys.save(str(fake_key_files["known_hosts"]))

    collector = _make_collector(fake_key_files, policy="strict")
    monkeypatch.setattr(paramiko.SSHClient, "connect", _patch_connect_to_invoke_policy(server_key))
    monkeypatch.setattr(paramiko.SSHClient, "get_transport", _patch_get_transport(server_key))

    with collector._connect("10.0.0.5", "virtvalidate"):
        pass

    assert collector.last_host_key_event["action"] == "verified"
