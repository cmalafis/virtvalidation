"""Tests for the multi-key SSH service + /api/ssh-keys endpoints.

Coexists with ``test_ssh_key_api.py`` which tests the singleton appliance
key. The CRITICAL test here is :func:`test_create_does_not_leak_private_key`:
no API response, error body, or list row may contain the private key
bytes.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.core import config as cfg_mod
from app.models.ssh_key import SSHKey, SSHKeyStatus
from app.services import ssh_key_service


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def isolated_key_dir(tmp_path, monkeypatch):
    """Point ssh_key_path at a tmp dir so each test gets a clean PVC."""
    target = tmp_path / "keys" / "id_ed25519"
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_path", str(target))
    monkeypatch.setattr(cfg_mod.settings, "ssh_key_algorithm", "ed25519")
    monkeypatch.setattr(cfg_mod.settings, "fips_mode", False)
    return tmp_path / "keys"


# ---------------------------------------------------------------------------
# Service-level
# ---------------------------------------------------------------------------
def test_generate_keypair_writes_private_to_pvc_and_persists_metadata(
    isolated_key_dir, db_session
):
    row = ssh_key_service.generate_keypair(db_session, name="wave-1-key")

    assert row.id is not None
    assert row.name == "wave-1-key"
    assert row.status == SSHKeyStatus.active
    assert row.algorithm == "ed25519"
    assert row.public_key.startswith("ssh-ed25519 ")
    assert row.fingerprint.startswith("SHA256:")

    private_path = Path(row.private_key_path)
    assert private_path.is_file(), "private key should be written to disk"
    assert private_path.parent.name == "named"
    # Mode is best-effort; the umask-respecting open seeds 0600 at create.
    assert private_path.stat().st_mode & 0o077 == 0, "private key must be 0600"


def test_generate_keypair_does_not_store_private_bytes_in_row(
    isolated_key_dir, db_session
):
    row = ssh_key_service.generate_keypair(db_session, name="audit-test")
    body = json.dumps(
        {
            "name": row.name,
            "public_key": row.public_key,
            "private_key_path": row.private_key_path,
            "fingerprint": row.fingerprint,
            "algorithm": row.algorithm,
        }
    )
    # Nothing about the row's columns should look like an SSH private key.
    assert "BEGIN OPENSSH PRIVATE KEY" not in body
    assert "PRIVATE KEY" not in body.upper().replace("PRIVATE_KEY_PATH", "")


def test_retire_key_marks_status_but_leaves_file_on_disk(isolated_key_dir, db_session):
    row = ssh_key_service.generate_keypair(db_session, name="retire-me")
    path = Path(row.private_key_path)
    assert path.is_file()

    ssh_key_service.retire_key(db_session, row.id)
    db_session.refresh(row)
    assert row.status == SSHKeyStatus.retired
    assert row.retired_at is not None
    # File survives for the audit window.
    assert path.is_file(), "retiring must not delete the on-disk private key"


def test_load_paramiko_key_loads_active_keys(isolated_key_dir, db_session):
    row = ssh_key_service.generate_keypair(db_session, name="loadable")
    key = ssh_key_service.load_paramiko_key(db_session, row.id)
    # The key object should expose its fingerprint and match the row.
    import base64
    import hashlib

    digest = hashlib.sha256(key.asbytes()).digest()
    fp = "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")
    assert fp == row.fingerprint


def test_load_paramiko_key_refuses_retired_keys(isolated_key_dir, db_session):
    row = ssh_key_service.generate_keypair(db_session, name="r")
    ssh_key_service.retire_key(db_session, row.id)
    with pytest.raises(ssh_key_service.SSHKeyRetiredError):
        ssh_key_service.load_paramiko_key(db_session, row.id)


def test_extract_blob_returns_middle_field():
    # Internal helper sanity — needed for the day-2 revocation match.
    line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIblob virtvalidate-appliance"
    assert ssh_key_service._extract_blob(line) == "AAAAC3NzaC1lZDI1NTE5AAAAIblob"
    assert ssh_key_service._extract_blob("garbage") is None


# ---------------------------------------------------------------------------
# API-level — these run through the FastAPI TestClient with a clean DB
# ---------------------------------------------------------------------------
def test_create_ssh_key_endpoint_returns_201_with_public_material(
    isolated_key_dir, client
):
    r = client.post("/api/ssh-keys", json={"name": "via-api"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["name"] == "via-api"
    assert body["status"] == "active"
    assert body["public_key"].startswith("ssh-ed25519 ")
    assert body["fingerprint"].startswith("SHA256:")
    assert body["algorithm"] == "ed25519"
    assert "private_key_path" not in body, "path is internal — never expose"


def test_create_does_not_leak_private_key(isolated_key_dir, client):
    """CRITICAL: no API response body may carry private key material.

    Iterates all four "key-bearing" endpoints (create, list, detail,
    public). For each, asserts:
      - no "PRIVATE KEY" substring (covers OpenSSH + PEM markers)
      - no "BEGIN OPENSSH" / "BEGIN RSA"-style marker
      - no path string for ``private_key_path`` (the path is internal,
        the bytes are even more internal)
    """
    r = client.post("/api/ssh-keys", json={"name": "leak-check"})
    assert r.status_code == 201, r.text
    key_id = r.json()["id"]

    candidates = [
        r.text,
        client.get("/api/ssh-keys").text,
        client.get(f"/api/ssh-keys/{key_id}").text,
        client.get(f"/api/ssh-keys/{key_id}/public").text,
    ]
    for body in candidates:
        upper = body.upper()
        assert "BEGIN OPENSSH PRIVATE KEY" not in upper, body[:200]
        assert "BEGIN RSA PRIVATE KEY" not in upper, body[:200]
        assert "BEGIN EC PRIVATE KEY" not in upper, body[:200]
        assert "BEGIN PRIVATE KEY" not in upper, body[:200]
        # The `private_key_path` field name itself must never appear in
        # any response — even though the path string isn't the key, leaking
        # filesystem locations breaks the seal.
        assert "private_key_path" not in body, body[:200]


def test_list_ssh_keys_returns_wrapped_response(isolated_key_dir, client):
    """List endpoint must follow {items, total, skip, limit} convention."""
    for i in range(3):
        client.post("/api/ssh-keys", json={"name": f"k{i}"})
    r = client.get("/api/ssh-keys")
    body = r.json()
    assert set(body) == {"items", "total", "skip", "limit"}
    assert body["total"] == 3
    assert len(body["items"]) == 3
    # Newest first.
    assert body["items"][0]["name"] == "k2"


def test_list_filter_by_status(isolated_key_dir, client):
    a = client.post("/api/ssh-keys", json={"name": "a"}).json()["id"]
    client.post("/api/ssh-keys", json={"name": "b"})
    client.post(f"/api/ssh-keys/{a}/retire")

    active = client.get("/api/ssh-keys?status=active").json()
    retired = client.get("/api/ssh-keys?status=retired").json()
    assert active["total"] == 1
    assert retired["total"] == 1
    assert active["items"][0]["name"] == "b"
    assert retired["items"][0]["name"] == "a"


def test_get_public_endpoint_returns_text_plain(isolated_key_dir, client):
    r = client.post("/api/ssh-keys", json={"name": "pub"})
    key_id = r.json()["id"]
    pub = client.get(f"/api/ssh-keys/{key_id}/public")
    assert pub.status_code == 200
    assert pub.headers["content-type"].startswith("text/plain")
    assert pub.text.strip().startswith("ssh-ed25519 ")


def test_get_playbook_returns_503_when_file_missing(isolated_key_dir, client, monkeypatch):
    """Playbook file ships in Part 6. Until then the endpoint returns 503."""
    from app.api import ssh_keys as ssh_keys_module

    monkeypatch.setattr(
        ssh_keys_module, "_PLAYBOOK_PATH", Path("/nonexistent/path.yml")
    )
    r = client.post("/api/ssh-keys", json={"name": "pb"})
    key_id = r.json()["id"]
    pb = client.get(f"/api/ssh-keys/{key_id}/playbook")
    assert pb.status_code == 503


def test_get_playbook_templates_public_key_when_file_present(
    isolated_key_dir, client, tmp_path, monkeypatch
):
    """When the playbook file exists, the endpoint substitutes the public
    key into the ``virtvalidate_public_key: "REPLACE_ME"`` default."""
    from app.api import ssh_keys as ssh_keys_module

    playbook = tmp_path / "fake-playbook.yml"
    playbook.write_text(
        "---\n"
        "- hosts: all\n"
        "  vars:\n"
        '    virtvalidate_public_key: "REPLACE_ME"\n'
    )
    monkeypatch.setattr(ssh_keys_module, "_PLAYBOOK_PATH", playbook)

    r = client.post("/api/ssh-keys", json={"name": "pb-render"})
    body = r.json()
    rendered = client.get(f"/api/ssh-keys/{body['id']}/playbook")
    assert rendered.status_code == 200
    assert rendered.headers["content-type"].startswith("text/yaml")
    assert body["public_key"].strip() in rendered.text
    assert "REPLACE_ME" not in rendered.text


def test_real_setup_playbook_file_renders_correctly(isolated_key_dir, client):
    """End-to-end: the real Ansible playbook file in deploy/ansible/ ships
    with the REPLACE_ME marker; the endpoint should substitute the public
    key in. Pins the contract between the playbook file and the route."""
    r = client.post("/api/ssh-keys", json={"name": "real-pb"})
    body = r.json()
    rendered = client.get(f"/api/ssh-keys/{body['id']}/playbook")
    assert rendered.status_code == 200, rendered.text
    assert rendered.headers["content-type"].startswith("text/yaml")
    # Sentinel must NOT appear in the served output — substitution worked.
    assert "REPLACE_ME" not in rendered.text
    assert body["public_key"].strip() in rendered.text
    # Sanity that the actual playbook was served (not some stripped stub).
    assert "virtvalidate" in rendered.text
    assert "/etc/sudoers.d/virtvalidate" in rendered.text
    # Content-Disposition is set so a browser save-as has a sensible name.
    assert "attachment" in rendered.headers.get("content-disposition", "")


def test_retire_endpoint_idempotent(isolated_key_dir, client):
    key_id = client.post("/api/ssh-keys", json={"name": "x"}).json()["id"]
    first = client.post(f"/api/ssh-keys/{key_id}/retire").json()
    second = client.post(f"/api/ssh-keys/{key_id}/retire").json()
    assert first["status"] == "retired"
    assert second["status"] == "retired"
    # Retired_at should be set on the first call; the second call must not
    # rewrite it (idempotent semantics).
    assert first["retired_at"] == second["retired_at"]


def test_create_rejects_unsupported_algorithm(isolated_key_dir, client):
    r = client.post("/api/ssh-keys", json={"name": "bad", "algorithm": "dsa"})
    assert r.status_code == 400
    assert "Unsupported" in r.json()["detail"]


def test_get_404_when_key_does_not_exist(isolated_key_dir, client):
    r = client.get("/api/ssh-keys/999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Day-2 revocation — service-level, paramiko mocked
# ---------------------------------------------------------------------------
def test_revoke_from_vms_per_vm_failures_do_not_abort_batch(
    isolated_key_dir, db_session, monkeypatch
):
    """A single unreachable VM must not prevent the rest of the batch from
    processing. We mock the internal helper so we don't need real SSH."""
    row = ssh_key_service.generate_keypair(db_session, name="revoke-test")

    calls = []

    def fake_revoke(**kwargs):
        calls.append(kwargs["host"])
        if kwargs["host"] == "bad":
            return "connect_failed:simulated"
        return None

    monkeypatch.setattr(ssh_key_service, "_revoke_pubkey_on_host", fake_revoke)
    targets = [
        {"vm_id": 1, "host": "ok-1", "port": 22, "username": "virtvalidate"},
        {"vm_id": 2, "host": "bad", "port": 22, "username": "virtvalidate"},
        {"vm_id": 3, "host": "ok-2", "port": 22, "username": "virtvalidate"},
    ]
    outcomes = ssh_key_service.revoke_key_from_vms(
        db_session, key_id=row.id, vm_targets=targets
    )
    assert [o.vm_id for o in outcomes] == [1, 2, 3]
    assert [o.succeeded for o in outcomes] == [True, False, True]
    assert outcomes[1].detail == "connect_failed:simulated"
    assert calls == ["ok-1", "bad", "ok-2"]


def test_revoke_from_vms_exception_does_not_abort_batch(
    isolated_key_dir, db_session, monkeypatch
):
    row = ssh_key_service.generate_keypair(db_session, name="rev-exc")

    def boom(**kwargs):
        if kwargs["host"] == "explode":
            raise RuntimeError("kaboom")
        return None

    monkeypatch.setattr(ssh_key_service, "_revoke_pubkey_on_host", boom)
    outcomes = ssh_key_service.revoke_key_from_vms(
        db_session,
        key_id=row.id,
        vm_targets=[
            {"vm_id": 1, "host": "ok", "port": 22, "username": "v"},
            {"vm_id": 2, "host": "explode", "port": 22, "username": "v"},
            {"vm_id": 3, "host": "ok2", "port": 22, "username": "v"},
        ],
    )
    assert outcomes[0].succeeded is True
    assert outcomes[1].succeeded is False
    assert "kaboom" in (outcomes[1].detail or "")
    assert outcomes[2].succeeded is True
