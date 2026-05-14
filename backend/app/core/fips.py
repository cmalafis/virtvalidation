"""FIPS 140-3 compliance helpers.

Federal customers (DoD, civilian agencies, FedRAMP) require the
appliance to operate inside a FIPS 140-3 boundary. The cryptographic
modules themselves are not implemented in VirtValidate — every crypto
operation goes through OpenSSL via the host OS or its bundled
``cryptography`` library. The host OS is authoritative for *whether*
those modules run in FIPS mode.

This module owns:

  - **Detection** of the host OS's FIPS posture
    (``/proc/sys/crypto/fips_enabled``).
  - **Configuration** — whether the operator asked us to enforce FIPS
    rules at the application layer (the ``FIPS_MODE`` env var).
  - **Enforcement** — gates that reject FIPS-noncompliant
    configurations (currently: SSH key algorithms).
  - **Reporting** — a structured status payload surfaced via
    ``/api/system/fips-status`` and embedded in ``/api/health/full``
    so operators (and federal reviewers) can see the posture at a
    glance.

We intentionally do NOT crash hard when ``FIPS_MODE=true`` is set on a
non-FIPS host. The configured intent and the detected reality are
both reported; operators decide whether to fix the host or relax the
flag. Crashing would lock operators out of the very dashboard they
need to diagnose the misconfiguration.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.core.config import settings as _module_settings

logger = logging.getLogger(__name__)

#: Algorithms approved under FIPS 186-5 for SSH host/user keys.
#: Ed25519 is intentionally NOT in this set — it's not yet approved
#: by NIST for FIPS 186-5 (revision pending). See FIPS_DEPLOYMENT.md
#: for the operator-facing discussion.
FIPS_APPROVED_KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-rsa",  # plus the FIPS minimum modulus check below
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        # ecdsa-sha2-nistp521 is also FIPS-approved but rare in practice;
        # we accept it the same way at the size check.
        "ecdsa-sha2-nistp521",
    }
)

#: Keys with names matching these prefixes are NEVER FIPS-approved
#: regardless of length (Ed25519 today; future legacy types as they're
#: deprecated). Listed explicitly so the rejection message is clear.
FIPS_REJECTED_KEY_TYPES: frozenset[str] = frozenset(
    {
        "ssh-ed25519",
        "ssh-dss",  # DSA — deprecated, never FIPS 186-5 approved
    }
)

#: Minimum RSA modulus in bits per FIPS 186-5.
FIPS_MIN_RSA_BITS = 3072

#: Hash algorithms allowed in FIPS mode (informational — used for
#: status reporting, not enforcement, since we don't compute hashes
#: outside SHA-256 today).
FIPS_APPROVED_HASHES: frozenset[str] = frozenset(
    {"sha-256", "sha-384", "sha-512", "sha-3-256", "sha-3-512"}
)

_FIPS_PROC_PATH = Path("/proc/sys/crypto/fips_enabled")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def is_os_fips_enabled(proc_path: Path | None = None) -> bool:
    """Return True iff the host OS is running with the FIPS kernel
    flag enabled.

    Reads ``/proc/sys/crypto/fips_enabled``. Returns False on any read
    failure (file missing on non-Linux, permission denied, etc.) — the
    safe interpretation is "we cannot prove FIPS is on, so it isn't."
    """
    path = proc_path or _FIPS_PROC_PATH
    try:
        return path.read_text().strip() == "1"
    except OSError:
        return False


def is_fips_mode(cfg=None) -> bool:
    """Return True iff the operator asked the application to enforce
    FIPS rules (regardless of whether the OS actually supports them)."""
    cfg = cfg or _module_settings
    return bool(getattr(cfg, "fips_mode", False))


# ---------------------------------------------------------------------------
# SSH key gate
# ---------------------------------------------------------------------------
class FIPSViolation(RuntimeError):
    """Raised when a FIPS-mode gate rejects a configuration or input.

    Subclass of ``RuntimeError`` so existing error handling at the SSH
    layer surfaces it without special-casing. The message is operator-
    facing — it MUST explain what was rejected and what to do instead,
    so federal reviewers reading audit logs can see the chain of
    cause and remediation.
    """


def assess_ssh_key(key_type: str, key_size_bits: int | None = None) -> dict:
    """Evaluate a loaded SSH key against FIPS 186-5.

    Pure function — no I/O, no exceptions raised. Returns a dict with:

      - ``approved``  — bool
      - ``reason``    — short string explaining the verdict
      - ``key_type``  — echo back for logging convenience
      - ``key_size_bits`` — echo back

    The caller decides whether to enforce: ``validate_ssh_key`` raises
    when fips_mode is on; reporting endpoints call this directly so
    they can show the verdict without throwing.
    """
    name = (key_type or "").lower()
    if name in FIPS_REJECTED_KEY_TYPES:
        return {
            "approved": False,
            "reason": (
                f"{name} is not approved under FIPS 186-5. "
                "Use RSA ≥3072 bits or ECDSA P-256/P-384/P-521 instead."
            ),
            "key_type": name,
            "key_size_bits": key_size_bits,
        }
    if name == "ssh-rsa":
        if key_size_bits is None:
            return {
                "approved": False,
                "reason": "Cannot determine RSA modulus size; refuse to assume.",
                "key_type": name,
                "key_size_bits": None,
            }
        if key_size_bits < FIPS_MIN_RSA_BITS:
            return {
                "approved": False,
                "reason": (
                    f"RSA key is {key_size_bits} bits; FIPS 186-5 requires "
                    f"at least {FIPS_MIN_RSA_BITS} bits."
                ),
                "key_type": name,
                "key_size_bits": key_size_bits,
            }
        return {
            "approved": True,
            "reason": f"RSA-{key_size_bits} satisfies FIPS 186-5.",
            "key_type": name,
            "key_size_bits": key_size_bits,
        }
    if name in FIPS_APPROVED_KEY_TYPES:
        return {
            "approved": True,
            "reason": f"{name} is approved under FIPS 186-5.",
            "key_type": name,
            "key_size_bits": key_size_bits,
        }
    return {
        "approved": False,
        "reason": f"Unrecognized key type {name!r}; refuse to assume FIPS approval.",
        "key_type": name,
        "key_size_bits": key_size_bits,
    }


def validate_ssh_key(key_type: str, key_size_bits: int | None = None, *, cfg=None) -> None:
    """Enforce :func:`assess_ssh_key` when ``fips_mode`` is active.

    No-op when fips_mode is off — operators running in non-FIPS
    deployments are free to use Ed25519. When fips_mode is on, raises
    :class:`FIPSViolation` with the verdict reason as the message.
    """
    if not is_fips_mode(cfg):
        return
    verdict = assess_ssh_key(key_type, key_size_bits)
    if verdict["approved"]:
        return
    raise FIPSViolation(f"SSH key rejected under FIPS_MODE=true: {verdict['reason']}")


# ---------------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------------
def fips_status(cfg=None) -> dict[str, Any]:
    """Build the structured status payload exposed to operators.

    ``configured`` is what the operator asked for (env var). ``detected``
    is whether the host OS is actually running with the FIPS kernel
    flag. ``effective`` is the AND of the two — only when both line up
    is the deployment legitimately FIPS-compliant.

    The ``mismatch_warning`` field is populated when the operator asked
    for FIPS but the host doesn't support it; the Settings UI surfaces
    it prominently so operators know why their compliance attestation
    won't pass.
    """
    cfg = cfg or _module_settings
    configured = is_fips_mode(cfg)
    detected = is_os_fips_enabled()
    effective = configured and detected

    operations = [
        {
            "name": "SSH key algorithm",
            "configured": getattr(cfg, "ssh_key_algorithm", "ed25519"),
            "fips_approved": _ssh_algo_is_approved(getattr(cfg, "ssh_key_algorithm", "ed25519")),
            "enforced": configured,
        },
        {
            "name": "OpenSSL (host)",
            "configured": "system openssl",
            "fips_approved": detected,
            "enforced": detected,
        },
        {
            "name": "Hash algorithms",
            "configured": "SHA-256 only",
            "fips_approved": True,
            "enforced": True,
        },
        {
            "name": "TLS — KServe backend",
            "configured": "verify_ssl=" + str(getattr(cfg, "kserve_verify_ssl", True)),
            # We always keep verify on by default; a False configured value
            # is an operator override and is loud-reported as a non-compliance.
            "fips_approved": bool(getattr(cfg, "kserve_verify_ssl", True)),
            "enforced": configured,
        },
    ]

    warning = None
    if configured and not detected:
        warning = (
            "FIPS_MODE=true is set but the host OS is not running with FIPS "
            "kernel mode enabled. Crypto operations will not be FIPS-validated "
            "even though application-level gates are active. Run "
            "`fips-mode-setup --enable` on the host (RHEL) or redeploy on "
            "RHCOS with `fips: true` in the install-config."
        )
    elif detected and not configured:
        warning = (
            "Host OS is in FIPS mode but FIPS_MODE=false in the application "
            "config — non-FIPS-approved key algorithms will load without "
            "warning. Set FIPS_MODE=true in the deployment env to align "
            "application-level gates with the host's posture."
        )

    return {
        "configured": configured,
        "detected": detected,
        "effective": effective,
        "mismatch_warning": warning,
        "operations": operations,
    }


def _ssh_algo_is_approved(algo: str) -> bool:
    a = (algo or "").lower()
    if a == "ed25519":
        return False
    if a in {"rsa-3072", "rsa-4096", "ecdsa-p256", "ecdsa-p384", "ecdsa-p521"}:
        return True
    return False


def log_startup_warning() -> None:
    """Emit a one-shot startup log line summarizing the FIPS posture.

    Called from ``app.main.lifespan`` so federal deployments leave a
    boot-time breadcrumb in the container logs without operators having
    to hit the API to find out.
    """
    s = fips_status()
    if s["effective"]:
        logger.info("FIPS 140-3: configured + OS-detected. Application gates active.")
    elif s["configured"] and not s["detected"]:
        logger.warning(
            "FIPS 140-3 mismatch — application configured for FIPS_MODE=true "
            "but host OS not in FIPS mode. %s",
            s["mismatch_warning"],
        )
    elif s["detected"] and not s["configured"]:
        logger.warning(
            "FIPS 140-3 mismatch — host OS in FIPS mode but FIPS_MODE=false "
            "in app config. Application-level gates inactive."
        )
    else:
        logger.info("FIPS 140-3: not configured (host non-FIPS).")
