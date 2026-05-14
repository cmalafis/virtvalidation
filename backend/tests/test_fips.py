"""FIPS 140-3 compliance tests.

Covers:

  - The pure-function ``assess_ssh_key`` verdict logic (no I/O, no
    config dependency).
  - The ``validate_ssh_key`` enforcement gate — no-op when fips_mode
    is off, raises when on.
  - OS detection from a temp ``/proc/sys/crypto/fips_enabled`` analog.
  - The ``fips_status`` reporting payload — including the
    mismatch-warning logic for misconfigured deployments.
  - The HTTP surface: ``/api/system/fips-status`` and the ``fips``
    block in ``/api/health/full``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.core.config import Settings
from app.core.fips import (
    FIPS_MIN_RSA_BITS,
    FIPSViolation,
    assess_ssh_key,
    fips_status,
    is_os_fips_enabled,
    validate_ssh_key,
)


# ---------------------------------------------------------------------------
# Pure verdict logic
# ---------------------------------------------------------------------------
class TestAssessSSHKey:
    def test_rejects_ed25519_with_clear_remediation(self):
        v = assess_ssh_key("ssh-ed25519", 256)
        assert v["approved"] is False
        assert "RSA" in v["reason"] and "ECDSA" in v["reason"]
        assert v["key_type"] == "ssh-ed25519"

    def test_rejects_ssh_dss(self):
        v = assess_ssh_key("ssh-dss", 1024)
        assert v["approved"] is False

    def test_accepts_rsa_3072(self):
        v = assess_ssh_key("ssh-rsa", 3072)
        assert v["approved"] is True
        assert "3072" in v["reason"]

    def test_accepts_rsa_4096(self):
        v = assess_ssh_key("ssh-rsa", 4096)
        assert v["approved"] is True

    def test_rejects_rsa_2048_below_minimum(self):
        v = assess_ssh_key("ssh-rsa", 2048)
        assert v["approved"] is False
        assert str(FIPS_MIN_RSA_BITS) in v["reason"]

    def test_rejects_rsa_with_unknown_size(self):
        v = assess_ssh_key("ssh-rsa", None)
        assert v["approved"] is False
        assert "modulus size" in v["reason"]

    def test_accepts_ecdsa_p384(self):
        v = assess_ssh_key("ecdsa-sha2-nistp384", 384)
        assert v["approved"] is True

    def test_accepts_ecdsa_p256(self):
        v = assess_ssh_key("ecdsa-sha2-nistp256", 256)
        assert v["approved"] is True

    def test_rejects_unknown_key_type(self):
        v = assess_ssh_key("ssh-cosmic-ray", 999)
        assert v["approved"] is False

    def test_case_insensitive(self):
        v = assess_ssh_key("SSH-ED25519", 256)
        assert v["approved"] is False


# ---------------------------------------------------------------------------
# Enforcement gate
# ---------------------------------------------------------------------------
class TestValidateSSHKey:
    def test_no_op_when_fips_mode_off(self):
        cfg = Settings(fips_mode=False)
        # Even an obviously bad key passes when fips_mode is off — we're
        # not in the federal-compliance posture, so no enforcement.
        validate_ssh_key("ssh-ed25519", 256, cfg=cfg)
        validate_ssh_key("ssh-rsa", 1024, cfg=cfg)

    def test_rejects_ed25519_when_fips_mode_on(self):
        cfg = Settings(fips_mode=True)
        with pytest.raises(FIPSViolation, match="not approved under FIPS"):
            validate_ssh_key("ssh-ed25519", 256, cfg=cfg)

    def test_accepts_rsa_3072_when_fips_mode_on(self):
        cfg = Settings(fips_mode=True)
        validate_ssh_key("ssh-rsa", 3072, cfg=cfg)

    def test_accepts_ecdsa_p384_when_fips_mode_on(self):
        cfg = Settings(fips_mode=True)
        validate_ssh_key("ecdsa-sha2-nistp384", 384, cfg=cfg)

    def test_rejects_rsa_2048_when_fips_mode_on(self):
        cfg = Settings(fips_mode=True)
        with pytest.raises(FIPSViolation, match=str(FIPS_MIN_RSA_BITS)):
            validate_ssh_key("ssh-rsa", 2048, cfg=cfg)

    def test_violation_message_is_operator_facing(self):
        cfg = Settings(fips_mode=True)
        with pytest.raises(FIPSViolation) as exc_info:
            validate_ssh_key("ssh-ed25519", 256, cfg=cfg)
        # Message must explain WHAT was rejected and HOW to fix it.
        msg = str(exc_info.value)
        assert "FIPS_MODE=true" in msg
        assert "RSA" in msg or "ECDSA" in msg


# ---------------------------------------------------------------------------
# OS detection
# ---------------------------------------------------------------------------
class TestOSDetection:
    def test_returns_true_when_proc_file_says_1(self, tmp_path):
        proc = tmp_path / "fips_enabled"
        proc.write_text("1\n")
        assert is_os_fips_enabled(proc) is True

    def test_returns_false_when_proc_file_says_0(self, tmp_path):
        proc = tmp_path / "fips_enabled"
        proc.write_text("0\n")
        assert is_os_fips_enabled(proc) is False

    def test_returns_false_when_proc_file_missing(self, tmp_path):
        # Non-Linux hosts and broken sysfs both produce a missing file —
        # the safe interpretation is "we cannot prove FIPS is on, so it
        # isn't."
        assert is_os_fips_enabled(tmp_path / "missing") is False

    def test_returns_false_on_unexpected_content(self, tmp_path):
        proc = tmp_path / "fips_enabled"
        proc.write_text("hello there\n")
        assert is_os_fips_enabled(proc) is False


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------
class TestFIPSStatus:
    def test_neither_configured_nor_detected(self, tmp_path):
        cfg = Settings(fips_mode=False)
        with patch("app.core.fips._FIPS_PROC_PATH", tmp_path / "missing"):
            s = fips_status(cfg)
        assert s["configured"] is False
        assert s["detected"] is False
        assert s["effective"] is False
        assert s["mismatch_warning"] is None

    def test_both_configured_and_detected_is_effective(self, tmp_path):
        proc = tmp_path / "fips_enabled"
        proc.write_text("1\n")
        cfg = Settings(fips_mode=True, ssh_key_algorithm="rsa-3072")
        with patch("app.core.fips._FIPS_PROC_PATH", proc):
            s = fips_status(cfg)
        assert s["configured"] is True
        assert s["detected"] is True
        assert s["effective"] is True
        assert s["mismatch_warning"] is None

    def test_configured_but_not_detected_warns(self, tmp_path):
        cfg = Settings(fips_mode=True)
        with patch("app.core.fips._FIPS_PROC_PATH", tmp_path / "missing"):
            s = fips_status(cfg)
        assert s["configured"] is True
        assert s["detected"] is False
        assert s["effective"] is False
        assert s["mismatch_warning"] is not None
        # Remediation must be specific enough that a federal reviewer
        # knows what to do without reading our source.
        assert "fips-mode-setup" in s["mismatch_warning"]

    def test_detected_but_not_configured_warns(self, tmp_path):
        proc = tmp_path / "fips_enabled"
        proc.write_text("1\n")
        cfg = Settings(fips_mode=False)
        with patch("app.core.fips._FIPS_PROC_PATH", proc):
            s = fips_status(cfg)
        assert s["configured"] is False
        assert s["detected"] is True
        assert s["effective"] is False
        assert s["mismatch_warning"] is not None
        assert "FIPS_MODE=true" in s["mismatch_warning"]

    def test_operations_includes_ssh_key_algorithm(self, tmp_path):
        cfg = Settings(fips_mode=True, ssh_key_algorithm="ed25519")
        with patch("app.core.fips._FIPS_PROC_PATH", tmp_path / "missing"):
            s = fips_status(cfg)
        ssh_op = next(op for op in s["operations"] if op["name"] == "SSH key algorithm")
        assert ssh_op["configured"] == "ed25519"
        # Ed25519 is NOT FIPS-approved under FIPS 186-5.
        assert ssh_op["fips_approved"] is False
        # Enforcement is on because operator asked for fips_mode.
        assert ssh_op["enforced"] is True

    def test_operations_marks_rsa_3072_as_approved(self, tmp_path):
        cfg = Settings(fips_mode=True, ssh_key_algorithm="rsa-3072")
        with patch("app.core.fips._FIPS_PROC_PATH", tmp_path / "missing"):
            s = fips_status(cfg)
        ssh_op = next(op for op in s["operations"] if op["name"] == "SSH key algorithm")
        assert ssh_op["fips_approved"] is True


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------
def test_fips_status_endpoint_returns_default_payload(client):
    r = client.get("/api/system/fips-status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert isinstance(body["detected"], bool)
    assert isinstance(body["operations"], list)
    op_names = {op["name"] for op in body["operations"]}
    assert "SSH key algorithm" in op_names
    assert "OpenSSL (host)" in op_names


def test_full_health_includes_fips_block(client, monkeypatch):
    """``/api/health/full`` must surface FIPS posture so federal load
    balancers and reviewers see it in the same probe as DB + LLM.

    The LLM probe is short-circuited via a stub backend so the test
    doesn't depend on a reachable Ollama; we're asserting the FIPS
    block, not LLM behavior.
    """
    from app.core.llm.base import LLMBackend

    class _Stub(LLMBackend):
        backend_type = "stub"
        default_model = "x"
        endpoint = "stub://"

        async def chat(self, messages, model=None, temperature=0.1, max_tokens=None):
            return {"content": "{}", "model": "x"}

        async def chat_stream(self, messages, model=None, temperature=0.1):  # pragma: no cover
            raise NotImplementedError
            if False:
                yield ""

        async def health_check(self):
            return {"status": "online", "backend": "stub", "latency_ms": 1}

        def list_models(self):
            return ["x"]

    stub = _Stub()
    monkeypatch.setattr("app.api.health.get_llm_backend", lambda: stub)

    r = client.get("/api/health/full")
    assert r.status_code == 200
    body = r.json()
    assert "fips" in body
    assert "configured" in body["fips"]
    assert "detected" in body["fips"]
    assert "operations" in body["fips"]


def test_fips_status_endpoint_warns_on_configured_without_detected(client, monkeypatch, tmp_path):
    """When the operator sets FIPS_MODE=true on a non-FIPS host, the
    endpoint must surface a remediation message — that's the federal
    reviewer's signal that the deployment isn't actually compliant."""
    monkeypatch.setattr("app.core.fips._module_settings.fips_mode", True)
    monkeypatch.setattr("app.core.fips._FIPS_PROC_PATH", tmp_path / "missing")

    r = client.get("/api/system/fips-status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["detected"] is False
    assert body["effective"] is False
    assert body["mismatch_warning"] is not None


# ---------------------------------------------------------------------------
# SSH integration — the gate fires through the collector path
# ---------------------------------------------------------------------------
def test_ssh_collector_rejects_ed25519_when_fips_mode_on(monkeypatch, tmp_path):
    """End-to-end: SSHCollector wraps validate_ssh_key, so an Ed25519
    key file under FIPS_MODE=true must surface SSHCollectionError before
    any network I/O happens. We mock the file read to skip generating
    a real key on disk and directly test the gate."""
    import paramiko

    from app.core import fips as fips_module
    from app.core.ssh import SSHCollectionError, SSHCollector

    # Force fips_mode on for the duration of the test.
    monkeypatch.setattr(fips_module._module_settings, "fips_mode", True)

    # Build a fake key file path that exists, then have load_private_key
    # return an Ed25519 stub so the gate fires on type, not on parse.
    fake_key = tmp_path / "id_ed25519"
    fake_key.write_text("not a real key but path must exist")

    class _StubEd25519:
        def get_name(self):
            return "ssh-ed25519"

        def asbytes(self):
            return b"stub"

    monkeypatch.setattr("app.core.ssh.load_private_key", lambda path: _StubEd25519())

    collector = SSHCollector(key_path=str(fake_key))
    with pytest.raises(SSHCollectionError, match="FIPS_MODE=true"):
        # _connect is a context manager; entering it triggers key load + gate.
        with collector._connect("10.0.0.5", "virtvalidate"):  # pragma: no cover
            pass

    # Sanity — paramiko import wasn't broken, RSAKey class still exists.
    assert hasattr(paramiko, "RSAKey")
