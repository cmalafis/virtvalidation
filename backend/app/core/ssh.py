"""
SSH Collection Engine.

Connects to VMs via Ed25519 keys and collects system state for pre-migration
baseline capture and post-migration validation.

OS-aware: the very first thing every collection does is detect the target
distro/version (see ``app.core.os_profile``). The detected ``OSProfile`` then
selects a ``CommandSet`` (see ``app.core.commands``) so RHEL 7 vs RHEL 9 vs
Debian differences are confined to a single dispatch table — the collection
methods themselves are the same on every OS.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import shlex
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional

import paramiko

from app.core.commands import CommandSet, command_set_for
from app.core.os_profile import UNKNOWN_PROFILE, OSProfile, detect

logger = logging.getLogger(__name__)

HostKeyPolicy = Literal["auto_accept", "strict"]


class SSHCollectionError(RuntimeError):
    """Raised when the SSH session cannot be established.

    ``kind`` lets callers (e.g. the audit-writing wrapper) distinguish
    routine failures from security-relevant ones:

      - ``host_key_mismatch`` — stored host key didn't match the one the
        server presented. Possible MITM or VM rebuild — must be audited
        loudly.
      - ``host_key_unknown`` — strict mode, host not in known_hosts.
        Operator must add it manually.
      - ``generic`` — anything else (auth, timeout, network).
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "generic",
        host: str = "",
        fingerprint: str = "",
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.host = host
        self.fingerprint = fingerprint


def _fingerprint_for(key: paramiko.PKey) -> str:
    """SHA-256 fingerprint in the OpenSSH ``SHA256:<base64>`` format.

    Matches what ``ssh-keygen -l -f`` emits, so operators comparing
    against the VM's own host key don't need a translation step.
    """
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")


class _RecordingAutoAddPolicy(paramiko.AutoAddPolicy):
    """AutoAddPolicy that captures *which* host key was added.

    Lets the collector emit a host_key_event so the wrapper can write an
    audit row without us having to diff known_hosts before/after.
    """

    def __init__(self, sink: list[dict]) -> None:
        super().__init__()
        self._sink = sink

    def missing_host_key(self, client, hostname, key):
        self._sink.append(
            {
                "action": "added",
                "host": hostname,
                "key_type": key.get_name(),
                "fingerprint": _fingerprint_for(key),
            }
        )
        return super().missing_host_key(client, hostname, key)


class SSHCollector:
    def __init__(
        self,
        key_path: str = "/app/keys/id_ed25519",
        port: int = 22,
        timeout: float = 15.0,
        command_timeout: float = 30.0,
        *,
        host_key_policy: HostKeyPolicy = "auto_accept",
        known_hosts_path: Optional[str] = None,
    ):
        self.key_path = Path(key_path)
        self.port = port
        self.timeout = timeout
        self.command_timeout = command_timeout
        self.host_key_policy: HostKeyPolicy = host_key_policy
        # Default known_hosts lives next to the private key. Same
        # /app/keys/ volume that's already mounted in podman-compose,
        # so persistence is free.
        self.known_hosts_path = (
            Path(known_hosts_path) if known_hosts_path else self.key_path.parent / "known_hosts"
        )
        # Populated by ``_connect`` after each call:
        #   {"action": "added"|"verified", "host": str,
        #    "key_type": str, "fingerprint": str}
        # The wrapper consults this after collect() to decide whether
        # to write an audit row.
        self.last_host_key_event: Optional[dict] = None

    def collect(self, host: str, username: str = "virtvalidate") -> dict:
        """SSH into a VM and collect full system state as structured JSON.

        Step 1 detects the OS and picks a ``CommandSet``. Every subsequent
        step uses the dispatched commands — the collector itself doesn't
        know whether it's talking to RHEL 7 or RHEL 9.
        """
        started = datetime.now(timezone.utc)
        with self._connect(host, username) as client:
            # ---- step 1: OS detection -----------------------------------
            os_profile = self._detect_os(client)
            cs = command_set_for(os_profile)
            if os_profile.detection_confidence == "low":
                logger.warning(
                    "OS detection unclear for %s — falling back to " "modern-Linux defaults",
                    host,
                )

            # ---- step 2: identity ---------------------------------------
            hostname = self._run(client, cs.hostname_fqdn).strip()

            # ---- step 3: per-subsystem collection ------------------------
            state = {
                "meta": {
                    "host": host,
                    "username": username,
                    "hostname": hostname,
                    # Legacy `os` block — kept for backward compatibility
                    # with snapshots written before OS-aware detection
                    # landed. Anything new should read `os_profile`.
                    "os": {
                        "id": os_profile.distro,
                        "version_id": os_profile.version_label,
                        "pretty_name": os_profile.pretty_name,
                    },
                    "kernel": os_profile.kernel_version,
                    "os_profile": os_profile.to_dict(),
                    "collected_at": started.isoformat(),
                },
                "services": self._collect_services(client, cs),
                "network": self._collect_network(client, cs),
                "ports": self._collect_ports(client, cs),
                "mounts": self._collect_mounts(client, cs),
                "cron": self._collect_cron(client, cs),
            }
        return state

    # -----------------------------------------------------------------
    # Connection plumbing
    # -----------------------------------------------------------------
    @contextmanager
    def _connect(self, host: str, username: str) -> Iterator[paramiko.SSHClient]:
        if not self.key_path.is_file():
            raise SSHCollectionError(f"SSH key not found at {self.key_path}")

        try:
            key = paramiko.Ed25519Key.from_private_key_file(str(self.key_path))
        except paramiko.SSHException as e:
            raise SSHCollectionError(f"Failed to load Ed25519 key: {e}") from e

        client = paramiko.SSHClient()
        client.load_system_host_keys()

        # Load our persistent known_hosts if it exists. First-ever boot
        # and a freshly mounted volume both produce a missing file; that
        # just means everyone is "new" until the first successful connect.
        if self.known_hosts_path.is_file():
            try:
                client.load_host_keys(str(self.known_hosts_path))
            except OSError as e:
                logger.warning(
                    "Could not read %s (%s); proceeding without persisted host keys",
                    self.known_hosts_path,
                    e,
                )

        # Reset the per-call event marker — only the most recent connect
        # wins, so the wrapper sees what actually just happened.
        self.last_host_key_event = None
        added_keys: list[dict] = []

        if self.host_key_policy == "strict":
            # Strict mode: paramiko's RejectPolicy refuses to connect to
            # any host not already in known_hosts. We translate the
            # exception into a SSHCollectionError with kind="host_key_unknown".
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(_RecordingAutoAddPolicy(added_keys))

        try:
            client.connect(
                hostname=host,
                port=self.port,
                username=username,
                pkey=key,
                timeout=self.timeout,
                auth_timeout=self.timeout,
                banner_timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except paramiko.BadHostKeyException as e:
            # The stored host key didn't match the one the server presented.
            # This is a real security event — a man-in-the-middle attack or
            # a legitimate VM rebuild — never silent.
            new_fp = _fingerprint_for(e.key)
            client.close()
            raise SSHCollectionError(
                (
                    f"Host key verification failed for {host}. The host key "
                    f"has changed since last connection — this could indicate "
                    f"a man-in-the-middle attack, or a legitimate VM rebuild. "
                    f"To accept the new key, remove the old entry from "
                    f"{self.known_hosts_path} and retry. To investigate, "
                    f"compare the current host key fingerprint with your "
                    f"records: {new_fp}"
                ),
                kind="host_key_mismatch",
                host=host,
                fingerprint=new_fp,
            ) from e
        except paramiko.SSHException as e:
            msg = str(e)
            # Strict mode + unknown host produces a "not found in known_hosts"
            # SSHException — translate into a clear remediation message.
            if "not found in known_hosts" in msg.lower() or "no hostkey" in msg.lower():
                client.close()
                raise SSHCollectionError(
                    (
                        f"Strict host-key checking is enabled and {host} is "
                        f"not in {self.known_hosts_path}. Either switch the "
                        f"SSH host key policy to auto-accept in Settings, or "
                        f"add the key out-of-band:\n"
                        f"  ssh-keyscan -p {self.port} {host} "
                        f">> {self.known_hosts_path}"
                    ),
                    kind="host_key_unknown",
                    host=host,
                ) from e
            client.close()
            raise SSHCollectionError(
                f"SSH connection to {host} failed: {e}",
                host=host,
            ) from e
        except OSError as e:
            client.close()
            raise SSHCollectionError(
                f"SSH connection to {host} failed: {e}",
                host=host,
            ) from e

        # Connection succeeded. Record what just happened with the host key
        # and persist any newly-added entries to disk.
        if added_keys:
            event = added_keys[0]
            self.last_host_key_event = event
            try:
                self.known_hosts_path.parent.mkdir(parents=True, exist_ok=True)
                client.save_host_keys(str(self.known_hosts_path))
            except OSError as e:
                # Logged but not fatal — the next collection will just
                # re-prompt the AutoAddPolicy. Common cause: read-only
                # mount in a misconfigured deployment.
                logger.warning(
                    "Could not persist host key to %s (%s); next collection "
                    "will accept this host again",
                    self.known_hosts_path,
                    e,
                )
        else:
            # Verified an existing entry — no audit row, but still useful
            # to surface the fingerprint to callers if they want to log
            # something (we don't, by default — too noisy).
            try:
                negotiated_key = client.get_transport().get_remote_server_key()
                self.last_host_key_event = {
                    "action": "verified",
                    "host": host,
                    "key_type": negotiated_key.get_name(),
                    "fingerprint": _fingerprint_for(negotiated_key),
                }
            except Exception:  # pragma: no cover — pure observability
                pass

        try:
            yield client
        finally:
            client.close()

    def _run(self, client: paramiko.SSHClient, command: str) -> str:
        """Execute a command and return stdout. Empty string on non-zero exit."""
        _, stdout, _stderr = client.exec_command(command, timeout=self.command_timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        if exit_code != 0:
            return ""
        return out

    # -----------------------------------------------------------------
    # OS detection
    # -----------------------------------------------------------------
    def _detect_os(self, client: paramiko.SSHClient) -> OSProfile:
        """First step of every collection. Best-effort: returns the
        ``UNKNOWN_PROFILE`` when /etc/os-release is unreadable so the
        rest of the collection can still proceed."""
        # We don't have a CommandSet yet (we're about to derive one), so
        # use the raw constants. These commands are stable across every
        # distro we care about.
        os_release = self._run(client, "cat /etc/os-release")
        if not os_release.strip():
            return UNKNOWN_PROFILE
        kernel = self._run(client, "uname -r").strip()
        arch = self._run(client, "uname -m").strip()
        return detect(os_release_text=os_release, uname_r=kernel, uname_m=arch)

    # -----------------------------------------------------------------
    # Per-subsystem collectors
    # -----------------------------------------------------------------
    def _collect_services(self, client: paramiko.SSHClient, cs: CommandSet) -> list[dict]:
        out = self._run(client, cs.services_running)
        services = []
        for line in out.splitlines():
            parts = line.split(None, 4)
            if len(parts) < 4:
                continue
            unit, load, active, sub, *rest = parts
            services.append(
                {
                    "unit": unit,
                    "load": load,
                    "active": active,
                    "sub": sub,
                    "description": rest[0] if rest else "",
                }
            )
        return services

    def _collect_network(self, client: paramiko.SSHClient, cs: CommandSet) -> dict:
        interfaces: dict[str, dict] = {}
        addr_out = self._run(client, cs.network_addr_v4)
        for line in addr_out.splitlines():
            # "2: eth0    inet 10.0.0.5/24 brd ..."
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
            if not m:
                continue
            iface, cidr = m.group(1), m.group(2)
            interfaces.setdefault(iface, {"ipv4": [], "ipv6": []})["ipv4"].append(cidr)

        addr6_out = self._run(client, cs.network_addr_v6)
        for line in addr6_out.splitlines():
            m = re.match(r"\d+:\s+(\S+)\s+inet6\s+(\S+)", line)
            if not m:
                continue
            iface, cidr = m.group(1), m.group(2)
            interfaces.setdefault(iface, {"ipv4": [], "ipv6": []})["ipv6"].append(cidr)

        routes = []
        route_out = self._run(client, cs.network_routes)
        for line in route_out.splitlines():
            line = line.strip()
            if line:
                routes.append(line)

        dns = []
        resolv = self._run(client, cs.resolv_conf)
        for line in resolv.splitlines():
            if line.startswith("nameserver"):
                parts = line.split()
                if len(parts) >= 2:
                    dns.append(parts[1])

        return {"interfaces": interfaces, "routes": routes, "dns": dns}

    def _collect_ports(self, client: paramiko.SSHClient, cs: CommandSet) -> list[dict]:
        out = self._run(client, cs.listening_ports)
        ports = []
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            proto, state, _recvq, _sendq, local = parts[0], parts[1], parts[2], parts[3], parts[4]
            addr, _, port = local.rpartition(":")
            if not port:
                continue
            ports.append(
                {
                    "proto": proto,
                    "state": state,
                    "address": addr,
                    "port": int(port) if port.isdigit() else port,
                }
            )
        return ports

    def _collect_mounts(self, client: paramiko.SSHClient, cs: CommandSet) -> list[dict]:
        out = self._run(client, cs.mounts)
        mounts = []
        for line in out.splitlines():
            parts = line.split(None, 5)
            if len(parts) < 4:
                continue
            target, source, fstype, options, *rest = parts
            if fstype in {"proc", "sysfs", "cgroup", "cgroup2", "devpts", "tmpfs", "devtmpfs"}:
                continue
            entry = {
                "target": target,
                "source": source,
                "fstype": fstype,
                "options": options,
            }
            if len(rest) >= 1:
                entry["size"] = rest[0]
            if len(rest) >= 2:
                entry["used"] = rest[1]
            mounts.append(entry)
        return mounts

    def _collect_cron(self, client: paramiko.SSHClient, cs: CommandSet) -> dict:
        users_out = self._run(client, cs.cron_users)
        user_crons: dict[str, list[str]] = {}
        for user in users_out.splitlines():
            user = user.strip()
            if not user:
                continue
            cmd = cs.cron_user_template.format(user=shlex.quote(user))
            body = self._run(client, cmd)
            entries = [
                ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
            ]
            if entries:
                user_crons[user] = entries

        system_entries: list[dict] = []
        sys_listing = self._run(client, cs.cron_system_paths)
        for path in sys_listing.splitlines():
            path = path.strip()
            if not path:
                continue
            body = self._run(client, f"cat {shlex.quote(path)}")
            entries = [
                ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
            ]
            if entries:
                system_entries.append({"path": path, "entries": entries})

        return {"user_crontabs": user_crons, "system": system_entries}
