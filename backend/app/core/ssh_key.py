"""SSH key generation + rotation for the appliance.

The Settings UI calls into this module to materialize an SSH keypair
on the mounted /app/keys volume without requiring the operator to
shell into the pod. Federal deployments lock pod shell down, so a
self-service generate flow is the only practical path.

Why use ``cryptography`` directly instead of ``ssh-keygen`` subprocess:

  - UBI nginx + python images don't ship the ssh-keygen binary; pulling
    in openssh-clients pads the layer and widens the supply chain.
  - Subprocess error handling is messier than catching a Python
    exception — and we already pay the ``cryptography`` dependency
    through paramiko.
  - Subprocess output (PEM, OpenSSH-format public key) is easy to
    misparse; the library returns the canonical bytes directly.

Algorithm support mirrors what the SSH collector loads:
  - ``ed25519`` — fastest, smallest, not FIPS-approved.
  - ``rsa``     — RSA 3072 bits, FIPS 186-5 compliant.
  - ``ecdsa``   — ECDSA P-384 (secp384r1), FIPS 186-5 compliant.

Pre-flight checks:

  - If a key already exists at the target path, ``generate`` refuses
    (returns the existing key's info as part of a ``KeyExistsError``).
    The Settings UI catches that and offers the rotate workflow.
  - When ``FIPS_MODE=true`` and the requested algorithm is Ed25519,
    ``validate_algorithm_for_fips`` raises before any key material is
    produced — federal deployments can't accidentally generate a
    non-compliant key via this endpoint.

Rotation:

  - The existing private + public files are renamed to
    ``<path>.bak.<unix-timestamp>`` (and the matching ``.pub`` file).
  - Backup retention is the operator's responsibility — we don't auto-
    delete because rolling back a bad rotation is easier with the
    backup still in place. ``docs/SSH_KEY_GUIDE.md`` documents the
    suggested 30-day window.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from app.core.fips import FIPSViolation, is_fips_mode

logger = logging.getLogger(__name__)


# RFC 4253 + PKCS#1 names the OpenSSH "comment" line uses. Same comment
# string the SSH collector embeds when reporting back its identity so
# federal customers see a stable principal in their authorized_keys.
DEFAULT_COMMENT = "virtvalidate-appliance"

#: Allowed values for the ``SSH_KEY_ALGORITHM`` env var + the
#: ``algorithm`` field on the generate request. Anything else returns
#: 400 from the API layer.
SUPPORTED_ALGORITHMS = ("ed25519", "rsa", "ecdsa")

#: SSH key types that fail FIPS 186-5. Mirrored from ``app.core.fips``
#: so we don't have to import a private constant.
_NOT_FIPS_APPROVED = {"ed25519"}

#: RSA modulus we generate. Matches the FIPS 186-5 minimum and the
#: cryptography library's recommended default for new keys.
_RSA_BITS = 3072

#: ECDSA curve. P-384 (secp384r1) is the federal sweet spot — well
#: supported by paramiko on the appliance side and accepted by every
#: federal customer's authorized_keys policy.
_ECDSA_CURVE = ec.SECP384R1()


# ---------------------------------------------------------------------------
# Public errors
# ---------------------------------------------------------------------------
class KeyExistsError(RuntimeError):
    """Raised by ``generate`` when a key already exists at the target path."""

    def __init__(self, path: str, existing: "KeyInfo") -> None:
        super().__init__(f"SSH key already exists at {path}")
        self.path = path
        self.existing = existing


class KeyMissingError(RuntimeError):
    """Raised by ``rotate`` when there's no existing key to rotate."""

    def __init__(self, path: str) -> None:
        super().__init__(f"No SSH key found at {path}; use generate first")
        self.path = path


class UnsupportedAlgorithmError(ValueError):
    """Raised on ``algorithm`` values outside :data:`SUPPORTED_ALGORITHMS`."""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class KeyInfo:
    algorithm: str
    fingerprint: str
    public_key: str
    created_at: datetime
    path: str

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "fingerprint": self.fingerprint,
            "public_key": self.public_key,
            "created_at": self.created_at.isoformat(),
            "path": self.path,
        }


# ---------------------------------------------------------------------------
# FIPS gate
# ---------------------------------------------------------------------------
def validate_algorithm_for_fips(algorithm: str, *, cfg=None) -> None:
    """Reject FIPS-incompatible algorithms when ``FIPS_MODE=true``.

    Mirrors :func:`app.core.fips.validate_ssh_key` but pre-emptive —
    the caller hasn't loaded a key yet, only chosen an algorithm.
    """
    if not is_fips_mode(cfg):
        return
    if algorithm.lower() in _NOT_FIPS_APPROVED:
        raise FIPSViolation(
            f"SSH key algorithm {algorithm!r} is not approved under "
            "FIPS 186-5. Set SSH_KEY_ALGORITHM=rsa or ecdsa for FIPS "
            "deployments."
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def normalize_algorithm(algorithm: str | None, *, cfg=None) -> str:
    """Resolve + validate the algorithm string from request/env input.

    Lowercases, strips, falls back to the cfg-resolved default. Raises
    :class:`UnsupportedAlgorithmError` for anything outside
    :data:`SUPPORTED_ALGORITHMS`.
    """
    if algorithm:
        algo = algorithm.strip().lower()
    else:
        from app.core.config import settings as default_cfg

        cfg = cfg or default_cfg
        algo = (getattr(cfg, "ssh_key_algorithm", None) or "ed25519").strip().lower()
    if algo not in SUPPORTED_ALGORITHMS:
        raise UnsupportedAlgorithmError(
            f"Unsupported SSH key algorithm {algo!r}. "
            f"Choose one of: {', '.join(SUPPORTED_ALGORITHMS)}."
        )
    return algo


def expected_path(algorithm: str, *, cfg=None) -> Path:
    """Resolve the on-disk path for a given algorithm.

    The configured ``SSH_KEY_PATH`` is treated as a directory-anchor —
    we replace its trailing filename with ``id_<algo>`` so RSA + ECDSA
    + Ed25519 keys cohabit cleanly. Existing deployments that set
    ``SSH_KEY_PATH=/app/keys/id_ed25519`` keep working: the directory
    stays the same, only the filename changes per algorithm.
    """
    from app.core.config import settings as default_cfg

    cfg = cfg or default_cfg
    base = Path(getattr(cfg, "ssh_key_path", "/app/keys/id_ed25519"))
    parent = base.parent
    return parent / f"id_{algorithm}"


def load_key_info(path: Path) -> KeyInfo | None:
    """Inspect an existing key + return its public-key info or None.

    Reads the ``.pub`` sidecar when present (fast path); falls back to
    loading the private key and re-deriving the public form if the
    sidecar was lost (e.g. operator manually scp'd in a key without
    its companion). Returns ``None`` when no key material exists.
    """
    pub_path = path.with_suffix(path.suffix + ".pub") if path.suffix else Path(str(path) + ".pub")
    raw: str | None = None
    if pub_path.is_file():
        raw = pub_path.read_text(encoding="utf-8").strip()
    elif path.is_file():
        raw = _derive_public_from_private(path)

    if not raw:
        return None

    parts = raw.split()
    if len(parts) < 2:
        return None
    key_type = parts[0]
    fingerprint = _fingerprint_from_b64(parts[1])
    try:
        stat = path.stat() if path.is_file() else pub_path.stat()
        created = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    except OSError:
        created = datetime.now(timezone.utc)
    return KeyInfo(
        algorithm=_ssh_type_to_algorithm(key_type),
        fingerprint=fingerprint,
        public_key=raw,
        created_at=created,
        path=str(path),
    )


def generate(
    algorithm: str | None = None,
    *,
    comment: str = DEFAULT_COMMENT,
    cfg=None,
) -> KeyInfo:
    """Materialize a new keypair at ``expected_path(algorithm)``.

    Refuses to overwrite an existing key — the caller distinguishes
    "first-time generate" from "rotate" via the endpoint URL, not by
    making generate accept --force. See :func:`rotate` for the
    backup-then-replace path.
    """
    algo = normalize_algorithm(algorithm, cfg=cfg)
    validate_algorithm_for_fips(algo, cfg=cfg)

    target = expected_path(algo, cfg=cfg)
    existing = load_key_info(target)
    if existing is not None:
        raise KeyExistsError(str(target), existing)

    target.parent.mkdir(parents=True, exist_ok=True)
    _write_keypair(algo, target, comment=comment)
    return load_key_info(target) or _raise_post_write_inconsistency(target)


def rotate(
    algorithm: str | None = None,
    *,
    comment: str = DEFAULT_COMMENT,
    cfg=None,
) -> tuple[KeyInfo, KeyInfo, list[str]]:
    """Replace the existing key with a freshly generated one.

    Returns ``(old_info, new_info, backup_paths)``. ``old_info`` is the
    info read before the rename — usable for the audit log. The
    private + public files of the previous key are renamed with a
    ``.bak.<unix-ts>`` suffix so a botched rotation can be rolled back
    by the operator (we don't auto-restore — manual is safer for the
    federal threat model).

    Raises :class:`KeyMissingError` when there's nothing to rotate —
    the caller should send the operator to the generate flow instead.
    """
    algo = normalize_algorithm(algorithm, cfg=cfg)
    validate_algorithm_for_fips(algo, cfg=cfg)

    # Walk the supported algorithms and find whichever key currently
    # exists. Operators can rotate from rsa → ecdsa, so we don't insist
    # the new algorithm matches the old one.
    old_info: KeyInfo | None = None
    old_path: Path | None = None
    for candidate in SUPPORTED_ALGORITHMS:
        path = expected_path(candidate, cfg=cfg)
        info = load_key_info(path)
        if info is not None:
            old_info = info
            old_path = path
            break

    if old_info is None or old_path is None:
        raise KeyMissingError(str(expected_path(algo, cfg=cfg)))

    timestamp = int(time.time())
    backups = _backup_pair(old_path, timestamp)

    target = expected_path(algo, cfg=cfg)
    # When the new algorithm == old algorithm, the original files were
    # renamed out of the way, so generation lands cleanly.
    target.parent.mkdir(parents=True, exist_ok=True)
    _write_keypair(algo, target, comment=comment)
    new_info = load_key_info(target) or _raise_post_write_inconsistency(target)
    return old_info, new_info, backups


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _backup_pair(path: Path, timestamp: int) -> list[str]:
    """Rename private + public to ``.bak.<timestamp>`` and return the new paths."""
    moved: list[str] = []
    pub = path.with_suffix(path.suffix + ".pub")
    if path.is_file():
        new_path = path.with_name(f"{path.name}.bak.{timestamp}")
        os.rename(path, new_path)
        moved.append(str(new_path))
    if pub.is_file():
        new_pub = pub.with_name(f"{pub.name}.bak.{timestamp}")
        os.rename(pub, new_pub)
        moved.append(str(new_pub))
    return moved


def _write_keypair(algorithm: str, target: Path, *, comment: str) -> None:
    """Produce the OpenSSH-format private + public files for the algorithm."""
    if algorithm == "ed25519":
        private_key = ed25519.Ed25519PrivateKey.generate()
    elif algorithm == "rsa":
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=_RSA_BITS)
    elif algorithm == "ecdsa":
        private_key = ec.generate_private_key(_ECDSA_CURVE)
    else:  # pragma: no cover — normalize_algorithm already gated
        raise UnsupportedAlgorithmError(algorithm)

    priv_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub = private_key.public_key()
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )

    # Write private with restrictive permissions BEFORE the data lands.
    # umask-respecting open() lets the bake-then-chmod sequence race
    # under a permissive umask; this seeds the file mode at create.
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(priv_bytes)
    except Exception:
        # Clean up partial writes so a retry can succeed.
        try:
            os.unlink(target)
        except OSError:
            pass
        raise
    os.chmod(target, 0o600)

    pub_path = target.with_suffix(target.suffix + ".pub")
    pub_with_comment = pub_bytes.decode("ascii").strip() + f" {comment}\n"
    pub_path.write_text(pub_with_comment, encoding="ascii")
    os.chmod(pub_path, 0o644)


def _derive_public_from_private(priv_path: Path) -> str | None:
    """Re-derive the OpenSSH public-key line when the .pub sidecar is gone.

    Walks the cryptography library's PEM loaders and emits the OpenSSH
    public-key representation, including a default comment. The SSH
    collector already does a similar trick via paramiko; this version
    avoids the paramiko import in the key-generation path.
    """
    try:
        with priv_path.open("rb") as f:
            priv = serialization.load_ssh_private_key(f.read(), password=None)
    except (ValueError, TypeError):
        try:
            with priv_path.open("rb") as f:
                priv = serialization.load_pem_private_key(f.read(), password=None)
        except (ValueError, TypeError):
            return None

    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return pub_bytes.decode("ascii").strip() + f" {DEFAULT_COMMENT}"


def _fingerprint_from_b64(b64: str) -> str:
    try:
        blob = b64decode(b64)
    except (ValueError, TypeError):
        return ""
    digest = hashlib.sha256(blob).digest()
    return "SHA256:" + b64encode(digest).decode("ascii").rstrip("=")


def _ssh_type_to_algorithm(ssh_type: str) -> str:
    """Map the OpenSSH wire-format type string back to our algorithm name."""
    s = ssh_type.lower()
    if s.startswith("ssh-ed25519"):
        return "ed25519"
    if s.startswith("ssh-rsa"):
        return "rsa"
    if s.startswith("ecdsa-sha2"):
        return "ecdsa"
    return s


def _raise_post_write_inconsistency(target: Path) -> KeyInfo:
    """Defensive — fires if writing succeeded but load_key_info fails.

    Shouldn't reach this code; if it does, the disk is in a weird state
    and the operator needs to clean up before retrying.
    """
    msg = (
        f"SSH key was written to {target} but couldn't be read back. "
        "Inspect the keys volume and remove partial files before "
        "retrying."
    )
    logger.error(msg)
    raise RuntimeError(msg)
