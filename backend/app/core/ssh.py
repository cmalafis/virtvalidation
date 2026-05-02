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

import json

import paramiko

from app.core.commands import CommandSet, command_set_for
from app.core.fips import FIPSViolation, validate_ssh_key
from app.core.os_profile import UNKNOWN_PROFILE, OSProfile, detect, detect_windows

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


def load_private_key(path: Path) -> paramiko.PKey:
    """Load a private key from disk, auto-detecting its type.

    Tries each paramiko key class in turn — we'd rather pay the cost of
    a few exceptions on startup than force operators to declare their
    key type up-front. Federal deployments swap from Ed25519 (default)
    to RSA-3072 / ECDSA P-384 by simply replacing the key file.

    Raises ``SSHCollectionError`` when no class can parse the file.
    """
    last_err: Exception | None = None
    for cls in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return cls.from_private_key_file(str(path))
        except paramiko.SSHException as e:
            last_err = e
            continue
    raise SSHCollectionError(
        f"Failed to load SSH key at {path} as Ed25519/RSA/ECDSA: {last_err}"
    )


def _key_size_bits(key: paramiko.PKey) -> int | None:
    """Best-effort key size in bits.

    Ed25519 keys are always 256 bits; RSA exposes ``size``; ECDSA
    exposes ``ecdsa_curve.key_length``. We return ``None`` when the
    type doesn't expose a size — the FIPS gate then rejects rather
    than guessing.
    """
    if isinstance(key, paramiko.RSAKey):
        return key.size
    if isinstance(key, paramiko.ECDSAKey):
        # paramiko stores the curve as an object with ``key_length``.
        curve = getattr(key, "ecdsa_curve", None)
        return getattr(curve, "key_length", None)
    if isinstance(key, paramiko.Ed25519Key):
        return 256
    return None


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
            # Windows and Linux share the same raw_data shape so the diff
            # engine + LLM prompt don't fork. The Windows branch produces
            # the Linux fields by normalizing PowerShell+JSON output, and
            # adds optional Windows-only blocks (hotfixes, ad_membership)
            # that stay null on Linux snapshots.
            state: dict = {
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
            }
            if os_profile.distro_family == "windows":
                state.update(self._collect_windows(client, cs))
            else:
                state.update(
                    {
                        "services": self._collect_services(client, cs),
                        "network": self._collect_network(client, cs),
                        "ports": self._collect_ports(client, cs),
                        "mounts": self._collect_mounts(client, cs),
                        "cron": self._collect_cron(client, cs),
                    }
                )
        return state

    # -----------------------------------------------------------------
    # Connection plumbing
    # -----------------------------------------------------------------
    @contextmanager
    def _connect(self, host: str, username: str) -> Iterator[paramiko.SSHClient]:
        if not self.key_path.is_file():
            raise SSHCollectionError(f"SSH key not found at {self.key_path}")

        key = load_private_key(self.key_path)
        # FIPS gate — rejects Ed25519 / undersized RSA / unrecognized
        # algorithms when fips_mode is on. No-op otherwise. We translate
        # the FIPSViolation into an SSHCollectionError so every call site
        # already handling SSH errors keeps working without special-casing.
        try:
            validate_ssh_key(key.get_name(), _key_size_bits(key))
        except FIPSViolation as e:
            raise SSHCollectionError(str(e)) from e

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
        """Execute a command and return stdout. Empty string on non-zero exit.

        PowerShell over OpenSSH on Windows occasionally emits UTF-16 LE
        with a BOM prefix; we strip both so the rest of the parsing
        layer always sees clean UTF-8 text without CRLF artifacts. A
        plain Linux session runs through this normalization as a no-op
        because UTF-8 BOMs are rare and CRLF→LF on stripped lines is safe.
        """
        _, stdout, _stderr = client.exec_command(command, timeout=self.command_timeout)
        exit_code = stdout.channel.recv_exit_status()
        raw = stdout.read()
        if exit_code != 0:
            return ""
        # PowerShell honors $OutputEncoding for stdout; its default differs
        # by Windows version — Server 2019 emits UTF-16 LE with a BOM,
        # Server 2022+ emits UTF-8 unless the operator's profile changed
        # it. We try UTF-16 first when we see the LE BOM, then fall back.
        if raw.startswith(b"\xff\xfe"):
            text = raw.decode("utf-16-le", errors="replace").lstrip("﻿")
        elif raw.startswith(b"\xfe\xff"):
            text = raw.decode("utf-16-be", errors="replace").lstrip("﻿")
        elif raw.startswith(b"\xef\xbb\xbf"):
            text = raw.decode("utf-8-sig", errors="replace")
        else:
            text = raw.decode("utf-8", errors="replace")
        # Normalize CRLF → LF so existing line-based parsers don't see
        # trailing \r artifacts on Windows output.
        return text.replace("\r\n", "\n").replace("\r", "\n")

    def _run_json(self, client: paramiko.SSHClient, command: str):
        """Run a command and parse its stdout as JSON.

        ConvertTo-Json sometimes emits a single object even when the
        operator's intent was a list (when the input pipeline produced
        exactly one item). We normalize that to a list at the call
        site as needed. Returns ``None`` on parse failure so the
        caller can degrade gracefully without raising.
        """
        out = self._run(client, command).strip()
        if not out:
            return None
        try:
            return json.loads(out)
        except (ValueError, TypeError):
            logger.warning(
                "Could not parse JSON output (%d bytes); first 120 chars: %r",
                len(out),
                out[:120],
            )
            return None

    # -----------------------------------------------------------------
    # OS detection
    # -----------------------------------------------------------------
    def _detect_os(self, client: paramiko.SSHClient) -> OSProfile:
        """First step of every collection. Best-effort: returns the
        ``UNKNOWN_PROFILE`` when neither POSIX nor Windows detection
        produces a usable profile.

        Detection order:
          1. POSIX: ``cat /etc/os-release``. Works for every Linux
             distro we care about.
          2. Windows: ``Get-CimInstance Win32_OperatingSystem``. Runs
             only when the POSIX probe returned nothing — saves a
             round-trip on the Linux happy path.

        On Windows hosts the POSIX probe either fails (cmd.exe doesn't
        recognize ``cat``) or returns empty (PowerShell ignores it
        silently), so the Windows branch fires naturally."""
        os_release = self._run(client, "cat /etc/os-release")
        if os_release.strip():
            kernel = self._run(client, "uname -r").strip()
            arch = self._run(client, "uname -m").strip()
            return detect(os_release_text=os_release, uname_r=kernel, uname_m=arch)

        # POSIX probe came back empty — try Windows.
        ps_probe = (
            'powershell -NoProfile -NonInteractive -Command "'
            "Get-CimInstance Win32_OperatingSystem | "
            'ConvertTo-Json -Compress -Depth 3"'
        )
        win_json = self._run(client, ps_probe).strip()
        if win_json:
            return detect_windows(get_ciminstance_json=win_json)
        return UNKNOWN_PROFILE

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

    # -----------------------------------------------------------------
    # Windows path — produces the same raw_data shape as the Linux
    # collectors above so the diff engine + LLM prompt don't fork on
    # OS family. Optional Windows-only fields (hotfixes, ad_membership)
    # ride alongside the shared fields.
    # -----------------------------------------------------------------
    def _collect_windows(self, client: paramiko.SSHClient, cs: CommandSet) -> dict:
        return {
            "services": self._collect_windows_services(client, cs),
            "network": self._collect_windows_network(client, cs),
            "ports": self._collect_windows_ports(client, cs),
            "mounts": self._collect_windows_volumes(client, cs),
            "cron": self._collect_windows_scheduled_tasks(client, cs),
            "hotfixes": self._collect_windows_hotfixes(client, cs),
            "ad_membership": self._collect_windows_ad_membership(client, cs),
        }

    def _collect_windows_services(
        self, client: paramiko.SSHClient, cs: CommandSet
    ) -> list[dict]:
        body = self._run_json(client, cs.services_running) or []
        if isinstance(body, dict):
            body = [body]
        services: list[dict] = []
        for svc in body:
            if not isinstance(svc, dict):
                continue
            # Normalize to the systemd-style fields the diff engine + LLM
            # prompt already understand. ``unit`` is the ServiceName,
            # ``description`` is the DisplayName.
            services.append(
                {
                    "unit": svc.get("Name") or "",
                    "load": "loaded",
                    # Get-Service Status is "Running" / "Stopped" / etc.
                    # Map to systemd's active/inactive vocabulary.
                    "active": (
                        "active"
                        if str(svc.get("Status") or "").lower() == "running"
                        else "inactive"
                    ),
                    "sub": str(svc.get("Status") or "").lower(),
                    "description": svc.get("DisplayName") or "",
                }
            )
        return services

    def _collect_windows_network(
        self, client: paramiko.SSHClient, cs: CommandSet
    ) -> dict:
        v4 = self._run_json(client, cs.network_addr_v4) or []
        v6 = self._run_json(client, cs.network_addr_v6) or []
        if isinstance(v4, dict):
            v4 = [v4]
        if isinstance(v6, dict):
            v6 = [v6]
        interfaces: dict[str, dict] = {}
        for entry in v4:
            if not isinstance(entry, dict):
                continue
            iface = entry.get("InterfaceAlias")
            ip = entry.get("IPAddress")
            prefix = entry.get("PrefixLength")
            if not iface or not ip:
                continue
            cidr = f"{ip}/{prefix}" if prefix is not None else ip
            interfaces.setdefault(iface, {"ipv4": [], "ipv6": []})["ipv4"].append(cidr)
        for entry in v6:
            if not isinstance(entry, dict):
                continue
            iface = entry.get("InterfaceAlias")
            ip = entry.get("IPAddress")
            prefix = entry.get("PrefixLength")
            if not iface or not ip:
                continue
            cidr = f"{ip}/{prefix}" if prefix is not None else ip
            interfaces.setdefault(iface, {"ipv4": [], "ipv6": []})["ipv6"].append(cidr)

        routes_raw = self._run_json(client, cs.network_routes) or []
        if isinstance(routes_raw, dict):
            routes_raw = [routes_raw]
        routes: list[str] = []
        for r in routes_raw:
            if not isinstance(r, dict):
                continue
            dest = r.get("DestinationPrefix") or "?"
            via = r.get("NextHop") or "?"
            iface = r.get("InterfaceAlias") or "?"
            routes.append(f"{dest} via {via} dev {iface}")

        dns_raw = self._run_json(client, cs.resolv_conf) or []
        if isinstance(dns_raw, dict):
            dns_raw = [dns_raw]
        dns: list[str] = []
        for d in dns_raw:
            if not isinstance(d, dict):
                continue
            servers = d.get("ServerAddresses") or []
            if isinstance(servers, str):
                servers = [servers]
            for s in servers:
                if s and s not in dns:
                    dns.append(s)
        return {"interfaces": interfaces, "routes": routes, "dns": dns}

    def _collect_windows_ports(
        self, client: paramiko.SSHClient, cs: CommandSet
    ) -> list[dict]:
        body = self._run_json(client, cs.listening_ports) or []
        if isinstance(body, dict):
            body = [body]
        ports: list[dict] = []
        for entry in body:
            if not isinstance(entry, dict):
                continue
            port = entry.get("LocalPort")
            try:
                port_val = int(port) if port is not None else 0
            except (ValueError, TypeError):
                port_val = 0
            if not port_val:
                continue
            ports.append(
                {
                    "proto": "tcp",  # Get-NetTCPConnection — UDP would need Get-NetUDPEndpoint
                    "state": "LISTEN",
                    "address": entry.get("LocalAddress") or "0.0.0.0",
                    "port": port_val,
                }
            )
        return ports

    def _collect_windows_volumes(
        self, client: paramiko.SSHClient, cs: CommandSet
    ) -> list[dict]:
        body = self._run_json(client, cs.mounts) or []
        if isinstance(body, dict):
            body = [body]
        mounts: list[dict] = []
        for vol in body:
            if not isinstance(vol, dict):
                continue
            drive = vol.get("DriveLetter")
            target = f"{drive}:\\" if drive else (vol.get("FileSystemLabel") or "?")
            entry = {
                "target": target,
                # No "source" concept on Windows volumes — populate with
                # the FileSystemLabel so the diff engine still has a
                # comparable field.
                "source": vol.get("FileSystemLabel") or "",
                "fstype": vol.get("FileSystemType") or "",
                "options": vol.get("HealthStatus") or "",
            }
            if vol.get("Size") is not None:
                entry["size"] = vol.get("Size")
            if vol.get("SizeRemaining") is not None:
                entry["used"] = vol.get("SizeRemaining")
            mounts.append(entry)
        return mounts

    def _collect_windows_scheduled_tasks(
        self, client: paramiko.SSHClient, cs: CommandSet
    ) -> dict:
        """Return scheduled tasks under the system_entries key so the
        existing diff engine compares them the same way it compares
        ``/etc/cron.d/*`` entries."""
        body = self._run_json(client, cs.cron_system_paths) or []
        if isinstance(body, dict):
            body = [body]
        # Group tasks under their TaskPath so diffing notices when a
        # whole path's worth of tasks moves or disappears.
        grouped: dict[str, list[str]] = {}
        for task in body:
            if not isinstance(task, dict):
                continue
            path = task.get("TaskPath") or "\\"
            name = task.get("TaskName")
            if not name:
                continue
            grouped.setdefault(path, []).append(name)
        system_entries = [
            {"path": path, "entries": sorted(entries)}
            for path, entries in sorted(grouped.items())
        ]
        return {"user_crontabs": {}, "system": system_entries}

    def _collect_windows_hotfixes(
        self, client: paramiko.SSHClient, cs: CommandSet
    ) -> list[dict]:
        if not cs.windows_hotfixes:
            return []
        body = self._run_json(client, cs.windows_hotfixes) or []
        if isinstance(body, dict):
            body = [body]
        out: list[dict] = []
        for hf in body:
            if not isinstance(hf, dict):
                continue
            installed = hf.get("InstalledOn")
            if isinstance(installed, dict):
                # Get-HotFix returns DateTime objects; ConvertTo-Json
                # serializes them as {"value": "/Date(…)/", "DateTime": "…"}.
                # Prefer the DateTime string when present.
                installed = installed.get("DateTime") or installed.get("value")
            out.append(
                {
                    "id": hf.get("HotFixID") or "",
                    "description": hf.get("Description") or "",
                    "installed_on": installed or "",
                }
            )
        return out

    def _collect_windows_ad_membership(
        self, client: paramiko.SSHClient, cs: CommandSet
    ) -> dict:
        if not cs.windows_ad_membership:
            return {}
        body = self._run_json(client, cs.windows_ad_membership) or {}
        if isinstance(body, list):
            body = body[0] if body else {}
        if not isinstance(body, dict):
            return {}
        return {
            "domain": body.get("Domain") or "",
            "part_of_domain": bool(body.get("PartOfDomain")),
            # DomainRole is an enum 0–5; surface the raw int and let the
            # LLM / dashboard interpret. 0=standalone workstation,
            # 1=member workstation, 2=standalone server, 3=member server,
            # 4=backup DC, 5=primary DC.
            "domain_role": body.get("DomainRole"),
        }
