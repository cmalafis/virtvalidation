"""
SSH Collection Engine
Connects to VMs via Ed25519 keys, collects system state for
pre-migration baseline capture and post-migration validation.
"""

from __future__ import annotations

import re
import shlex
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import paramiko


class SSHCollectionError(RuntimeError):
    """Raised when the SSH session cannot be established."""


class SSHCollector:
    def __init__(
        self,
        key_path: str = "/app/keys/id_ed25519",
        port: int = 22,
        timeout: float = 15.0,
        command_timeout: float = 30.0,
    ):
        self.key_path = Path(key_path)
        self.port = port
        self.timeout = timeout
        self.command_timeout = command_timeout

    def collect(self, host: str, username: str = "virtvalidate") -> dict:
        """SSH into a VM and collect full system state as structured JSON."""
        started = datetime.now(timezone.utc)
        with self._connect(host, username) as client:
            hostname = self._run(client, "hostname -f || hostname").strip()
            os_release = self._parse_os_release(self._run(client, "cat /etc/os-release"))
            kernel = self._run(client, "uname -r").strip()

            state = {
                "meta": {
                    "host": host,
                    "username": username,
                    "hostname": hostname,
                    "os": os_release,
                    "kernel": kernel,
                    "collected_at": started.isoformat(),
                },
                "services": self._collect_services(client),
                "network": self._collect_network(client),
                "ports": self._collect_ports(client),
                "mounts": self._collect_mounts(client),
                "cron": self._collect_cron(client),
            }
        return state

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
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
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
            yield client
        except (paramiko.SSHException, OSError) as e:
            raise SSHCollectionError(f"SSH connection to {host} failed: {e}") from e
        finally:
            client.close()

    def _run(self, client: paramiko.SSHClient, command: str) -> str:
        """Execute a command and return stdout. Empty string on non-zero exit."""
        _, stdout, stderr = client.exec_command(command, timeout=self.command_timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        if exit_code != 0:
            return ""
        return out

    def _collect_services(self, client: paramiko.SSHClient) -> list[dict]:
        out = self._run(
            client,
            "systemctl list-units --type=service --state=running " "--no-legend --no-pager --plain",
        )
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

    def _collect_network(self, client: paramiko.SSHClient) -> dict:
        interfaces: dict[str, dict] = {}
        addr_out = self._run(client, "ip -o -4 addr show")
        for line in addr_out.splitlines():
            # "2: eth0    inet 10.0.0.5/24 brd ..."
            m = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
            if not m:
                continue
            iface, cidr = m.group(1), m.group(2)
            interfaces.setdefault(iface, {"ipv4": [], "ipv6": []})["ipv4"].append(cidr)

        addr6_out = self._run(client, "ip -o -6 addr show")
        for line in addr6_out.splitlines():
            m = re.match(r"\d+:\s+(\S+)\s+inet6\s+(\S+)", line)
            if not m:
                continue
            iface, cidr = m.group(1), m.group(2)
            interfaces.setdefault(iface, {"ipv4": [], "ipv6": []})["ipv6"].append(cidr)

        routes = []
        route_out = self._run(client, "ip -4 route show")
        for line in route_out.splitlines():
            line = line.strip()
            if line:
                routes.append(line)

        dns = []
        resolv = self._run(client, "cat /etc/resolv.conf")
        for line in resolv.splitlines():
            if line.startswith("nameserver"):
                parts = line.split()
                if len(parts) >= 2:
                    dns.append(parts[1])

        return {"interfaces": interfaces, "routes": routes, "dns": dns}

    def _collect_ports(self, client: paramiko.SSHClient) -> list[dict]:
        out = self._run(client, "ss -tulnH")
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

    def _collect_mounts(self, client: paramiko.SSHClient) -> list[dict]:
        out = self._run(
            client,
            "findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,USED",
        )
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

    def _collect_cron(self, client: paramiko.SSHClient) -> dict:
        users_out = self._run(client, "cut -d: -f1 /etc/passwd")
        user_crons: dict[str, list[str]] = {}
        for user in users_out.splitlines():
            user = user.strip()
            if not user:
                continue
            body = self._run(client, f"crontab -l -u {shlex.quote(user)} 2>/dev/null")
            entries = [
                ln for ln in body.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
            ]
            if entries:
                user_crons[user] = entries

        system_entries: list[dict] = []
        sys_listing = self._run(
            client,
            "find /etc/cron.d /etc/crontab /etc/cron.hourly /etc/cron.daily "
            "/etc/cron.weekly /etc/cron.monthly -maxdepth 1 -type f 2>/dev/null",
        )
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

    @staticmethod
    def _parse_os_release(raw: str) -> dict:
        fields: dict[str, str] = {}
        for line in raw.splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            fields[k.strip()] = v.strip().strip('"')
        return {
            "id": fields.get("ID", ""),
            "version_id": fields.get("VERSION_ID", ""),
            "pretty_name": fields.get("PRETTY_NAME", ""),
        }
