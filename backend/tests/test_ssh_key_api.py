"""Tests for the SSH key self-service endpoints.

Pins the Settings page's three-state contract — exists / missing /
error — plus the FIPS gate, the conflict on re-generate, the backup-
and-replace semantics of rotate, and the audit trail every state
transition emits.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.core import config as cfg_mod
from app.core.ssh_key import (
    KeyExistsError,
    KeyMissingError,
    UnsupportedAlgorithmError,
    expected_path,
    generate,
    load_key_info,
    normalize_algorithm,
    rotate,
    validate_algorithm_for_fips,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_key_dir(tmp_path, monkeypatch):
    """Point ssh_key_path at a fresh tmp dir for each test.

    Both the config singleton and the per-call ``cfg`` argument resolve
    to this path; tests don't need to thread the path through manually.
    """
    target = tmp_path / "keys" / "id_ed25519"
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_path", str(target))
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_algorithm", "ed25519")
    return tmp_path / "keys"


@pytest.fixture
def fips_off(monkeypatch):
    monkeypatch.setattr(cfg_mod.settings, "fips_mode", False)


@pytest.fixture
def fips_on(monkeypatch):
    monkeypatch.setattr(cfg_mod.settings, "fips_mode", True)


# ---------------------------------------------------------------------------
# Core module — pure-function paths
# ---------------------------------------------------------------------------
def test_normalize_algorithm_defaults_to_configured(isolated_key_dir, fips_off, monkeypatch):
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_algorithm", "rsa")
    assert normalize_algorithm(None) == "rsa"


def test_normalize_algorithm_rejects_unknown(isolated_key_dir):
    with pytest.raises(UnsupportedAlgorithmError):
        normalize_algorithm("dsa")


def test_validate_algorithm_for_fips_blocks_ed25519(fips_on):
    from app.core.fips import FIPSViolation

    with pytest.raises(FIPSViolation):
        validate_algorithm_for_fips("ed25519")


def test_validate_algorithm_for_fips_allows_rsa(fips_on):
    validate_algorithm_for_fips("rsa")  # no raise
    validate_algorithm_for_fips("ecdsa")  # no raise


def test_validate_algorithm_for_fips_noop_when_fips_off(fips_off):
    validate_algorithm_for_fips("ed25519")  # no raise


# ---------------------------------------------------------------------------
# generate()
# ---------------------------------------------------------------------------
def test_generate_writes_keypair_with_correct_permissions(isolated_key_dir, fips_off):
    info = generate("ed25519")
    priv = Path(info.path)
    pub = priv.with_suffix(priv.suffix + ".pub")
    assert priv.is_file()
    assert pub.is_file()
    # 0o600 / 0o644 — operator-only on the private side.
    assert (priv.stat().st_mode & 0o777) == 0o600
    assert (pub.stat().st_mode & 0o777) == 0o644
    # Public key has the canonical comment.
    body = pub.read_text()
    assert body.startswith("ssh-ed25519 ")
    assert "virtvalidate-appliance" in body
    # Fingerprint is non-empty and SHA256-formatted.
    assert info.fingerprint.startswith("SHA256:")


def test_generate_refuses_when_key_exists(isolated_key_dir, fips_off):
    generate("ed25519")
    with pytest.raises(KeyExistsError) as exc:
        generate("ed25519")
    assert exc.value.existing.fingerprint.startswith("SHA256:")


def test_generate_rsa_3072(isolated_key_dir, fips_off):
    info = generate("rsa")
    assert info.algorithm == "rsa"
    body = Path(info.path).with_suffix(".pub").read_text()
    assert body.startswith("ssh-rsa ")


def test_generate_ecdsa_p384(isolated_key_dir, fips_off):
    info = generate("ecdsa")
    assert info.algorithm == "ecdsa"
    body = Path(info.path).with_suffix(".pub").read_text()
    # The OpenSSH wire name for P-384 is ecdsa-sha2-nistp384.
    assert body.startswith("ecdsa-sha2-nistp384 ")


def test_generate_under_fips_rejects_ed25519(isolated_key_dir, fips_on):
    from app.core.fips import FIPSViolation

    with pytest.raises(FIPSViolation):
        generate("ed25519")


def test_generate_under_fips_allows_rsa(isolated_key_dir, fips_on):
    info = generate("rsa")
    assert info.algorithm == "rsa"


# ---------------------------------------------------------------------------
# rotate()
# ---------------------------------------------------------------------------
def test_rotate_replaces_existing_key_and_backs_up(isolated_key_dir, fips_off):
    old = generate("ed25519")
    old_fp = old.fingerprint
    rotated_old, rotated_new, backups = rotate("ed25519")
    assert rotated_old.fingerprint == old_fp
    assert rotated_new.fingerprint != old_fp
    # Both private + public should have been backed up.
    backup_paths = [Path(p) for p in backups]
    assert any(p.name.startswith("id_ed25519.bak.") for p in backup_paths)
    assert any(p.name.startswith("id_ed25519.pub.bak.") for p in backup_paths)
    for p in backup_paths:
        assert p.is_file()


def test_rotate_when_no_existing_key_raises(isolated_key_dir, fips_off):
    with pytest.raises(KeyMissingError):
        rotate("ed25519")


def test_rotate_across_algorithms(isolated_key_dir, fips_off):
    # Start with ed25519; rotate into rsa — operators going FIPS-mode
    # mid-deployment hit this path.
    generate("ed25519")
    old, new, backups = rotate("rsa")
    assert old.algorithm == "ed25519"
    assert new.algorithm == "rsa"
    rsa_path = expected_path("rsa")
    assert rsa_path.is_file()


# ---------------------------------------------------------------------------
# load_key_info()
# ---------------------------------------------------------------------------
def test_load_key_info_returns_none_when_missing(isolated_key_dir):
    assert load_key_info(expected_path("ed25519")) is None


def test_load_key_info_uses_pub_sidecar_when_present(isolated_key_dir, fips_off):
    generate("ed25519")
    info = load_key_info(expected_path("ed25519"))
    assert info is not None
    assert info.algorithm == "ed25519"


def test_load_key_info_derives_pub_from_private_when_sidecar_missing(
    isolated_key_dir, fips_off,
):
    info = generate("ed25519")
    pub = Path(info.path).with_suffix(".pub")
    os.unlink(pub)
    derived = load_key_info(Path(info.path))
    assert derived is not None
    assert derived.algorithm == "ed25519"
    assert derived.fingerprint == info.fingerprint


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------
def test_get_ssh_key_returns_missing_status_when_no_key(client, isolated_key_dir):
    r = client.get("/api/system/ssh-key")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "missing"
    assert body["configured_algorithm"] == "ed25519"
    assert body["expected_path"].endswith("id_ed25519")


def test_get_ssh_key_returns_exists_status_after_generate(client, isolated_key_dir, fips_off):
    client.post("/api/system/ssh-key/generate", json={}).raise_for_status()
    r = client.get("/api/system/ssh-key")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "exists"
    assert body["algorithm"] == "ed25519"
    assert body["fingerprint"].startswith("SHA256:")
    assert body["public_key"].startswith("ssh-ed25519 ")


def test_post_generate_creates_key_and_returns_201(client, isolated_key_dir, fips_off):
    r = client.post("/api/system/ssh-key/generate", json={})
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["algorithm"] == "ed25519"
    assert body["fingerprint"].startswith("SHA256:")
    assert body["public_key"].startswith("ssh-ed25519 ")


def test_post_generate_409_when_key_exists(client, isolated_key_dir, fips_off):
    client.post("/api/system/ssh-key/generate", json={})
    r = client.post("/api/system/ssh-key/generate", json={})
    assert r.status_code == 409
    assert "rotate" in r.json()["detail"]


def test_post_generate_400_for_fips_violation(client, isolated_key_dir, fips_on):
    r = client.post("/api/system/ssh-key/generate", json={"algorithm": "ed25519"})
    assert r.status_code == 400
    assert "FIPS" in r.json()["detail"]


def test_post_generate_400_for_unknown_algorithm(client, isolated_key_dir, fips_off):
    r = client.post("/api/system/ssh-key/generate", json={"algorithm": "dsa"})
    assert r.status_code == 400
    assert "Unsupported" in r.json()["detail"]


def test_post_rotate_404_when_no_existing_key(client, isolated_key_dir, fips_off):
    r = client.post("/api/system/ssh-key/rotate", json={})
    assert r.status_code == 404


def test_post_rotate_replaces_key_and_returns_backup_info(client, isolated_key_dir, fips_off):
    first = client.post("/api/system/ssh-key/generate", json={}).json()
    r = client.post("/api/system/ssh-key/rotate", json={})
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["previous_fingerprint"] == first["fingerprint"]
    assert body["fingerprint"] != first["fingerprint"]
    assert isinstance(body["backups"], list)
    assert len(body["backups"]) >= 1
    assert "warning" in body
    assert "authorized_keys" in body["warning"]


def test_audit_entries_recorded_for_generate_rotate_view(client, isolated_key_dir, fips_off):
    client.post("/api/system/ssh-key/generate", json={})
    client.post("/api/system/ssh-key/rotate", json={})
    client.get("/api/system/ssh-key")
    audit = client.get("/api/audit?limit=100").json()
    actions = {a["action"] for a in audit}
    assert {"ssh.key_generated", "ssh.key_rotated", "ssh.key_viewed"} <= actions


def test_get_ssh_key_status_when_missing_does_not_log_key_viewed(client, isolated_key_dir):
    # No key + no view audit row — only the existence-confirming read
    # should emit the "ssh.key_viewed" event so federal reviewers don't
    # see noise from the empty-state Settings page.
    client.get("/api/system/ssh-key")
    audit = client.get("/api/audit?limit=100").json()
    actions = {a["action"] for a in audit}
    assert "ssh.key_viewed" not in actions
