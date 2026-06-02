"""Multi-key SSH service used by the wave-scoped baseline + validation flow.

This file does NOT modify or replace :mod:`app.core.ssh_key`. That module
still backs the singleton appliance key surfaced via
``/api/system/ssh-key``. This service backs the *named* multi-key catalog
surfaced via ``/api/ssh-keys`` — one row per generated key, with optional
per-plan scoping, an explicit retirement workflow, and a day-2 revocation
path that removes the public key from a list of VMs' ``authorized_keys``.

The PRIVATE KEY bytes NEVER appear in any API response, log line, or
error message. Only the filesystem path is recorded in the DB (see
:attr:`app.models.ssh_key.SSHKey.private_key_path`). The private file
lives at ``<keys_dir>/named/<uuid>`` with mode 0600.
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import paramiko
from sqlalchemy.orm import Session

from app.core.config import settings as app_config
from app.core.ssh_key import (
    DEFAULT_COMMENT,
    _write_keypair,
    load_key_info,
    normalize_algorithm,
    validate_algorithm_for_fips,
)
from app.models.ssh_key import SSHKey, SSHKeyStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class SSHKeyNotFoundError(LookupError):
    """The requested SSH key row doesn't exist (or has been deleted)."""


class SSHKeyRetiredError(RuntimeError):
    """The key exists but is retired; new runs may not use it."""


class PrivateKeyMissingError(RuntimeError):
    """The DB row exists but the private key file on disk is gone.

    This happens when the appliance PVC was lost or an operator manually
    deleted the file. The row needs to be retired and a new key generated.
    """


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
@dataclass
class VMRevocationOutcome:
    vm_id: int
    succeeded: bool
    detail: str | None = None


def keys_directory(*, cfg=None) -> Path:
    """Resolve the directory that hosts the named-key files.

    Mirrors the singleton's resolution: the configured ``SSH_KEY_PATH``'s
    parent directory is the keys volume root; named keys live in a
    ``named/`` subdirectory there. Co-locating means they ride the same
    PVC + the same backup/restore story as the singleton.
    """
    cfg = cfg or app_config
    base = Path(getattr(cfg, "ssh_key_path", "/app/keys/id_ed25519"))
    return base.parent / "named"


def generate_keypair(
    db: Session,
    *,
    name: str,
    plan_id: int | None = None,
    algorithm: str | None = None,
    comment: str | None = None,
    cfg=None,
) -> SSHKey:
    """Materialize a new keypair, write the private file to the PVC, and
    persist the metadata row.

    The private key NEVER touches the DB. The :class:`SSHKey` row only
    references the filesystem path; the file itself has mode 0600 and is
    only readable by the appliance UID.
    """
    cfg = cfg or app_config
    algo = normalize_algorithm(algorithm, cfg=cfg)
    validate_algorithm_for_fips(algo, cfg=cfg)

    directory = keys_directory(cfg=cfg)
    directory.mkdir(parents=True, exist_ok=True)
    # Best-effort directory perm tightening — emptyDir/PVC default modes
    # vary; this gives us a defense-in-depth check on top of the file
    # mode at write time.
    try:
        os.chmod(directory, 0o700)
    except OSError:
        # The container's UID may not own the mount root (PVC default).
        # The per-file 0600 below is the real protection.
        pass

    key_uuid = uuid.uuid4().hex
    target = directory / key_uuid
    _write_keypair(algo, target, comment=comment or DEFAULT_COMMENT)
    info = load_key_info(target)
    if info is None:  # pragma: no cover — _write_keypair raises on failure
        raise RuntimeError(
            f"Key was written to {target} but could not be read back; "
            "investigate the keys volume."
        )

    row = SSHKey(
        name=name,
        public_key=info.public_key,
        private_key_path=str(target),
        fingerprint=info.fingerprint,
        algorithm=algo,
        status=SSHKeyStatus.active,
        plan_id=plan_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    logger.info(
        "ssh_key.generated id=%s name=%s algorithm=%s fingerprint=%s plan_id=%s",
        row.id,
        row.name,
        row.algorithm,
        row.fingerprint,
        row.plan_id,
    )
    return row


def get_key(db: Session, key_id: int) -> SSHKey:
    row = db.get(SSHKey, key_id)
    if row is None:
        raise SSHKeyNotFoundError(f"SSH key {key_id} not found")
    return row


def get_active_key(db: Session, key_id: int) -> SSHKey:
    row = get_key(db, key_id)
    if row.status != SSHKeyStatus.active:
        raise SSHKeyRetiredError(f"SSH key {key_id} is retired")
    return row


def get_public_key(db: Session, key_id: int) -> str:
    """Return the OpenSSH public-key string. Safe to expose via API."""
    return get_key(db, key_id).public_key


def load_paramiko_key(db: Session, key_id: int) -> paramiko.PKey:
    """Load the paramiko key object for use by the collection orchestrator.

    Reads from the on-disk private key file referenced by the row. NEVER
    expose the return value via an API surface; this is for internal use
    by the orchestrator only.
    """
    row = get_active_key(db, key_id)
    path = Path(row.private_key_path)
    if not path.is_file():
        raise PrivateKeyMissingError(
            f"SSH key {key_id} ({row.name}) private file at {path} is "
            "missing; the row may need to be retired and the key regenerated."
        )

    # Try the algorithm hinted by the row first to save two failed loads
    # on the hot path; fall back to the legacy auto-detect chain otherwise.
    candidates: list[type[paramiko.PKey]]
    if row.algorithm == "ed25519":
        candidates = [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]
    elif row.algorithm == "rsa":
        candidates = [paramiko.RSAKey, paramiko.Ed25519Key, paramiko.ECDSAKey]
    elif row.algorithm == "ecdsa":
        candidates = [paramiko.ECDSAKey, paramiko.RSAKey, paramiko.Ed25519Key]
    else:
        candidates = [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]

    last_err: Exception | None = None
    for cls in candidates:
        try:
            return cls.from_private_key_file(str(path))
        except paramiko.SSHException as e:
            last_err = e
            continue
    raise PrivateKeyMissingError(
        f"SSH key {key_id} private file at {path} could not be loaded "
        f"as Ed25519/RSA/ECDSA: {last_err}"
    )


def retire_key(db: Session, key_id: int) -> SSHKey:
    """Mark a key retired. The private key file is left on disk for the
    audit window — a separate cleanup job (out of scope here) can purge
    retired keys older than N days.
    """
    row = get_key(db, key_id)
    if row.status == SSHKeyStatus.retired:
        return row
    row.status = SSHKeyStatus.retired
    row.retired_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    logger.info("ssh_key.retired id=%s name=%s", row.id, row.name)
    return row


# ---------------------------------------------------------------------------
# Day-2 revocation
# ---------------------------------------------------------------------------
def _revoke_pubkey_on_host(
    *,
    host: str,
    port: int,
    username: str,
    paramiko_key: paramiko.PKey,
    public_key_line: str,
    connect_timeout: float,
    command_timeout: float,
) -> str | None:
    """SSH into one VM and drop the matching pubkey line from authorized_keys.

    Returns ``None`` on success or a short error string on failure. NEVER
    raises — per-VM failures are surfaced as return values so a single
    unreachable host can't abort the batch.

    The match is on the base64 blob (middle field of the OpenSSH pubkey
    line) so a comment-string difference doesn't strand us. We compare
    blobs because that's the identity of the key; the leading type tag and
    the trailing comment are both metadata.
    """
    target_blob = _extract_blob(public_key_line)
    if not target_blob:
        return "invalid_public_key_format"

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        try:
            client.connect(
                hostname=host,
                port=port,
                username=username,
                pkey=paramiko_key,
                timeout=connect_timeout,
                auth_timeout=connect_timeout,
                banner_timeout=connect_timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.AuthenticationException:
            # The key we're trying to revoke is already not authorized.
            # Treat as success — the end state matches the operator's intent.
            return None
        except (paramiko.SSHException, OSError) as e:
            return f"connect_failed:{e}"

        # Read authorized_keys, filter, write back atomically. We use the
        # remote shell because SFTP requires a separate subsystem and not
        # every minimal container image enables it.
        sftp = None
        try:
            sftp = client.open_sftp()
            authorized_keys_path = ".ssh/authorized_keys"
            try:
                with sftp.open(authorized_keys_path, "r") as fh:
                    existing = fh.read().decode("utf-8", errors="replace")
            except IOError:
                # No authorized_keys file — nothing to remove.
                return None

            kept: list[str] = []
            removed = False
            for line in existing.splitlines():
                blob = _extract_blob(line)
                if blob == target_blob:
                    removed = True
                    continue
                kept.append(line)

            if not removed:
                # Key wasn't installed on this VM. Idempotent success.
                return None

            new_body = "\n".join(kept).rstrip("\n") + ("\n" if kept else "")
            # Write to a temp path, then rename — survives an aborted write.
            tmp_path = f"{authorized_keys_path}.virtvalidate-revoke"
            with sftp.open(tmp_path, "w") as fh:
                fh.write(new_body)
            try:
                sftp.chmod(tmp_path, 0o600)
            except IOError:
                pass
            try:
                # Some OpenSSH SFTP servers reject rename-over-existing; the
                # remove-then-rename two-step is broadly compatible.
                try:
                    sftp.remove(authorized_keys_path)
                except IOError:
                    pass
                sftp.rename(tmp_path, authorized_keys_path)
            except IOError as e:
                # Best-effort cleanup of the tmp file so we don't strand
                # an extra authorized_keys.virtvalidate-revoke around.
                try:
                    sftp.remove(tmp_path)
                except IOError:
                    pass
                return f"rename_failed:{e}"
            return None
        finally:
            if sftp is not None:
                try:
                    sftp.close()
                except Exception:  # noqa: BLE001 — close should never raise upward
                    pass
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass


def _extract_blob(public_key_line: str) -> str | None:
    """Return the base64 blob from an OpenSSH-format public key line.

    The line format is ``<type> <base64> [comment...]``. We return the
    middle field. Anything that doesn't look like a valid OpenSSH line
    (no whitespace, no recognizable type prefix) returns ``None``.
    """
    parts = public_key_line.strip().split()
    if len(parts) < 2:
        return None
    return parts[1]


def revoke_key_from_vms(
    db: Session,
    *,
    key_id: int,
    vm_targets: list[dict],
    cfg=None,
) -> list[VMRevocationOutcome]:
    """Remove a key's public component from each VM's authorized_keys.

    ``vm_targets`` is a list of dicts shaped
    ``{"vm_id": int, "host": str, "port": int, "username": str}``. The
    caller (the routes module) is responsible for resolving VM rows to
    these connection parameters — the service stays DB-agnostic about
    the VM model's specific column names.

    Per-VM failures NEVER raise; they become :class:`VMRevocationOutcome`
    entries with ``succeeded=False`` and a short ``detail``. A single
    unreachable VM cannot prevent the rest of the batch from running.
    """
    cfg = cfg or app_config
    row = get_key(db, key_id)
    paramiko_key = load_paramiko_key(db, key_id)
    connect_timeout = float(getattr(cfg, "ssh_connect_timeout_seconds", 10))
    command_timeout = float(getattr(cfg, "ssh_command_timeout_seconds", 30))

    outcomes: list[VMRevocationOutcome] = []
    for target in vm_targets:
        vm_id = int(target["vm_id"])
        host = str(target["host"])
        port = int(target.get("port", 22))
        username = str(target.get("username", "virtvalidate"))
        try:
            err = _revoke_pubkey_on_host(
                host=host,
                port=port,
                username=username,
                paramiko_key=paramiko_key,
                public_key_line=row.public_key,
                connect_timeout=connect_timeout,
                command_timeout=command_timeout,
            )
        except Exception as e:  # noqa: BLE001 — per-VM failures must never abort the batch
            outcomes.append(
                VMRevocationOutcome(vm_id=vm_id, succeeded=False, detail=f"unexpected:{e}")
            )
            continue
        if err is None:
            outcomes.append(VMRevocationOutcome(vm_id=vm_id, succeeded=True))
        else:
            outcomes.append(VMRevocationOutcome(vm_id=vm_id, succeeded=False, detail=err))
    return outcomes
