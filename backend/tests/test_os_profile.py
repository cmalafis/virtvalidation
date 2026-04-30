"""Tests for OS detection + per-OS command dispatch.

Sample ``/etc/os-release`` payloads are real ones from upstream — kept in
``OS_RELEASE_*`` constants below so it's obvious which strings the
collector is supposed to handle.
"""

from __future__ import annotations

import pytest

from app.core.commands import CommandSet, command_set_for
from app.core.os_profile import UNKNOWN_PROFILE, detect

# ---------------------------------------------------------------------------
# Sample os-release payloads
# ---------------------------------------------------------------------------
OS_RELEASE_RHEL_7 = """\
NAME="Red Hat Enterprise Linux Server"
VERSION="7.9 (Maipo)"
ID="rhel"
ID_LIKE="fedora"
VARIANT="Server"
VARIANT_ID="server"
VERSION_ID="7.9"
PRETTY_NAME="Red Hat Enterprise Linux Server 7.9 (Maipo)"
"""

OS_RELEASE_RHEL_8 = """\
NAME="Red Hat Enterprise Linux"
VERSION="8.10 (Ootpa)"
ID="rhel"
ID_LIKE="fedora"
VERSION_ID="8.10"
PLATFORM_ID="platform:el8"
PRETTY_NAME="Red Hat Enterprise Linux 8.10 (Ootpa)"
"""

OS_RELEASE_RHEL_9 = """\
NAME="Red Hat Enterprise Linux"
VERSION="9.4 (Plow)"
ID="rhel"
ID_LIKE="fedora"
VERSION_ID="9.4"
PLATFORM_ID="platform:el9"
PRETTY_NAME="Red Hat Enterprise Linux 9.4 (Plow)"
"""

OS_RELEASE_RHEL_10 = """\
NAME="Red Hat Enterprise Linux"
VERSION="10.0 (Coughlan)"
ID="rhel"
ID_LIKE="fedora"
VERSION_ID="10.0"
PLATFORM_ID="platform:el10"
PRETTY_NAME="Red Hat Enterprise Linux 10.0 (Coughlan)"
"""

OS_RELEASE_ROCKY_9 = """\
NAME="Rocky Linux"
VERSION="9.4 (Blue Onyx)"
ID="rocky"
ID_LIKE="rhel centos fedora"
VERSION_ID="9.4"
PLATFORM_ID="platform:el9"
PRETTY_NAME="Rocky Linux 9.4 (Blue Onyx)"
"""

OS_RELEASE_ALMA_9 = """\
NAME="AlmaLinux"
VERSION="9.4 (Seafoam Ocelot)"
ID="almalinux"
ID_LIKE="rhel centos fedora"
VERSION_ID="9.4"
PLATFORM_ID="platform:el9"
PRETTY_NAME="AlmaLinux 9.4 (Seafoam Ocelot)"
"""

OS_RELEASE_UBUNTU_2204 = """\
NAME="Ubuntu"
VERSION="22.04.4 LTS (Jammy Jellyfish)"
ID=ubuntu
ID_LIKE=debian
VERSION_ID="22.04"
PRETTY_NAME="Ubuntu 22.04.4 LTS"
"""

OS_RELEASE_DEBIAN_12 = """\
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
VERSION="12 (bookworm)"
ID=debian
"""

OS_RELEASE_GARBAGE = "this is not an os-release file"
OS_RELEASE_EMPTY = ""


# ---------------------------------------------------------------------------
# detect()
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected_distro,expected_major,expected_family,expected_confidence",
    [
        (OS_RELEASE_RHEL_7, "rhel", 7, "rhel-like", "high"),
        (OS_RELEASE_RHEL_8, "rhel", 8, "rhel-like", "high"),
        (OS_RELEASE_RHEL_9, "rhel", 9, "rhel-like", "high"),
        (OS_RELEASE_RHEL_10, "rhel", 10, "rhel-like", "high"),
        (OS_RELEASE_ROCKY_9, "rocky", 9, "rhel-like", "high"),
        (OS_RELEASE_ALMA_9, "alma", 9, "rhel-like", "high"),
        (OS_RELEASE_UBUNTU_2204, "ubuntu", 22, "debian-like", "high"),
        (OS_RELEASE_DEBIAN_12, "debian", 12, "debian-like", "high"),
    ],
)
def test_detect_recognizes_canonical_distros(
    raw, expected_distro, expected_major, expected_family, expected_confidence
):
    profile = detect(os_release_text=raw, uname_r="5.14.0-362.el9", uname_m="x86_64")
    assert profile.distro == expected_distro
    assert profile.major_version == expected_major
    assert profile.distro_family == expected_family
    assert profile.detection_confidence == expected_confidence
    assert profile.is_systemd is True


def test_detect_parses_minor_version():
    profile = detect(os_release_text=OS_RELEASE_RHEL_9, uname_r="", uname_m="")
    assert profile.minor_version == 4
    assert profile.version_label == "9.4"


def test_detect_falls_back_to_unknown_on_garbage():
    profile = detect(os_release_text=OS_RELEASE_GARBAGE, uname_r="", uname_m="")
    assert profile.distro == "unknown"
    assert profile.distro_family == "unknown"
    assert profile.detection_confidence == "low"


def test_detect_falls_back_to_unknown_on_empty():
    profile = detect(os_release_text=OS_RELEASE_EMPTY, uname_r="", uname_m="")
    assert profile.distro == "unknown"
    assert profile.detection_confidence == "low"


def test_detect_records_kernel_and_arch():
    profile = detect(
        os_release_text=OS_RELEASE_RHEL_9,
        uname_r="5.14.0-362.el9.x86_64",
        uname_m="x86_64",
    )
    assert profile.kernel_version == "5.14.0-362.el9.x86_64"
    assert profile.architecture == "x86_64"


def test_unknown_profile_singleton_is_safe_default():
    assert UNKNOWN_PROFILE.distro == "unknown"
    assert UNKNOWN_PROFILE.detection_confidence == "low"
    # Always systemd-by-assumption so the modern default CommandSet works.
    assert UNKNOWN_PROFILE.is_systemd is True


def test_to_dict_round_trips_for_snapshot_meta():
    profile = detect(os_release_text=OS_RELEASE_RHEL_9, uname_r="5.14", uname_m="x86_64")
    d = profile.to_dict()
    assert d["distro"] == "rhel"
    assert d["major_version"] == 9
    assert d["detection_confidence"] == "high"
    assert d["is_systemd"] is True


# ---------------------------------------------------------------------------
# command_set_for() dispatch
# ---------------------------------------------------------------------------
def _cs(raw: str) -> CommandSet:
    return command_set_for(detect(os_release_text=raw, uname_r="", uname_m=""))


def test_rhel_7_uses_legacy_ss_and_yum():
    cs = _cs(OS_RELEASE_RHEL_7)
    # ss(8) on RHEL 7 doesn't support -H — must strip header manually.
    assert "ss -tuln" in cs.listening_ports
    assert "-H" not in cs.listening_ports
    assert cs.package_list.startswith("yum ")
    assert "iptables" in cs.firewall_inspect


def test_rhel_8_uses_modern_ss_and_dnf():
    cs = _cs(OS_RELEASE_RHEL_8)
    assert cs.listening_ports == "ss -tulnH"
    assert cs.package_list.startswith("dnf ")
    assert "nft list ruleset" in cs.firewall_inspect


def test_rhel_9_uses_modern_ss_and_dnf():
    cs = _cs(OS_RELEASE_RHEL_9)
    assert cs.listening_ports == "ss -tulnH"
    assert cs.package_list.startswith("dnf ")


def test_rhel_10_uses_modern_dispatch():
    cs = _cs(OS_RELEASE_RHEL_10)
    assert cs.listening_ports == "ss -tulnH"
    assert "chronyc" in cs.time_sync_status


def test_rocky_9_routes_through_rhel_modern_dispatch():
    cs = _cs(OS_RELEASE_ROCKY_9)
    assert cs.package_list.startswith("dnf ")
    assert cs.listening_ports == "ss -tulnH"


def test_alma_9_routes_through_rhel_modern_dispatch():
    cs = _cs(OS_RELEASE_ALMA_9)
    assert cs.package_list.startswith("dnf ")
    assert cs.listening_ports == "ss -tulnH"


def test_ubuntu_routes_through_debian_dispatch():
    cs = _cs(OS_RELEASE_UBUNTU_2204)
    assert cs.package_list.startswith("dpkg-query")
    assert "ufw" in cs.firewall_inspect


def test_unknown_os_falls_back_to_modern_defaults():
    cs = _cs(OS_RELEASE_GARBAGE)
    # The modern-default CommandSet equals the RHEL modern one.
    assert cs.listening_ports == "ss -tulnH"


# ---------------------------------------------------------------------------
# SSHCollector dispatch — mock the whole client so we can capture commands
# ---------------------------------------------------------------------------
class _StubClient:
    """Records every command the collector tries to run; returns canned
    responses keyed by exact command string. Keys match what the modern
    RHEL CommandSet uses; the test asserts the collector calls them."""

    def __init__(self, responses: dict[str, str]):
        self.responses = responses
        self.commands: list[str] = []


class _StubChannel:
    def __init__(self, exit_code: int = 0):
        self._exit_code = exit_code

    def recv_exit_status(self) -> int:
        return self._exit_code


class _StubStdout:
    def __init__(self, text: str):
        self._text = text
        self.channel = _StubChannel(0)

    def read(self) -> bytes:
        return self._text.encode("utf-8")


def _exec(stub: _StubClient, command: str, timeout: float = 0):
    stub.commands.append(command)
    text = stub.responses.get(command, "")
    return None, _StubStdout(text), _StubStdout("")


def test_collector_runs_os_detection_first_then_dispatched_commands(monkeypatch):
    """Verify SSHCollector calls /etc/os-release + uname -r/-m before any
    other command, then uses RHEL-9 dispatch for the rest."""
    from app.core.ssh import SSHCollector

    responses = {
        "cat /etc/os-release": OS_RELEASE_RHEL_9,
        "uname -r": "5.14.0-362.el9.x86_64\n",
        "uname -m": "x86_64\n",
        "hostname -f || hostname": "db-01.corp\n",
        "systemctl list-units --type=service --state=running --no-legend --no-pager --plain": "",
        "ip -o -4 addr show": "",
        "ip -o -6 addr show": "",
        "ip -4 route show": "",
        "cat /etc/resolv.conf": "",
        "ss -tulnH": "",
        "findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,USED": "",
        "cut -d: -f1 /etc/passwd": "",
        (
            "find /etc/cron.d /etc/crontab /etc/cron.hourly /etc/cron.daily "
            "/etc/cron.weekly /etc/cron.monthly -maxdepth 1 -type f 2>/dev/null"
        ): "",
    }
    stub = _StubClient(responses)

    # Replace exec_command on the stub to feed our canned responses.
    stub.exec_command = lambda cmd, timeout=0: _exec(stub, cmd, timeout)

    # _connect is a context manager — patch it to yield our stub.
    from contextlib import contextmanager

    @contextmanager
    def fake_connect(self, host, username):
        yield stub

    monkeypatch.setattr(SSHCollector, "_connect", fake_connect)

    collector = SSHCollector(key_path="/dev/null")
    state = collector.collect(host="10.0.0.5", username="virtvalidate")

    # First three commands MUST be the OS detection trio, in order.
    assert stub.commands[0] == "cat /etc/os-release"
    assert stub.commands[1] == "uname -r"
    assert stub.commands[2] == "uname -m"

    # The dispatch should have picked the modern-RHEL listening_ports.
    assert "ss -tulnH" in stub.commands
    assert "ss -tuln | tail -n +2" not in stub.commands

    # Snapshot meta carries the OSProfile.
    profile = state["meta"]["os_profile"]
    assert profile["distro"] == "rhel"
    assert profile["major_version"] == 9
    assert profile["detection_confidence"] == "high"
    assert profile["kernel_version"] == "5.14.0-362.el9.x86_64"


def test_collector_uses_rhel7_legacy_ss_when_target_is_rhel7(monkeypatch):
    from app.core.ssh import SSHCollector

    responses = {
        "cat /etc/os-release": OS_RELEASE_RHEL_7,
        "uname -r": "3.10.0-1160.el7.x86_64\n",
        "uname -m": "x86_64\n",
        "hostname -f || hostname": "db-01.corp\n",
        "systemctl list-units --type=service --state=running --no-legend --no-pager --plain": "",
        "ip -o -4 addr show": "",
        "ip -o -6 addr show": "",
        "ip -4 route show": "",
        "cat /etc/resolv.conf": "",
        "ss -tuln | tail -n +2": "",
        "findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,USED": "",
        "cut -d: -f1 /etc/passwd": "",
        (
            "find /etc/cron.d /etc/crontab /etc/cron.hourly /etc/cron.daily "
            "/etc/cron.weekly /etc/cron.monthly -maxdepth 1 -type f 2>/dev/null"
        ): "",
    }
    stub = _StubClient(responses)
    stub.exec_command = lambda cmd, timeout=0: _exec(stub, cmd, timeout)

    from contextlib import contextmanager

    @contextmanager
    def fake_connect(self, host, username):
        yield stub

    monkeypatch.setattr(SSHCollector, "_connect", fake_connect)

    collector = SSHCollector(key_path="/dev/null")
    state = collector.collect(host="10.0.0.5", username="virtvalidate")

    # Legacy ss path, NOT the modern one.
    assert "ss -tuln | tail -n +2" in stub.commands
    assert "ss -tulnH" not in stub.commands
    assert state["meta"]["os_profile"]["major_version"] == 7


def test_collector_falls_back_to_modern_defaults_for_unknown_os(monkeypatch):
    from app.core.ssh import SSHCollector

    responses = {
        "cat /etc/os-release": OS_RELEASE_GARBAGE,
        "uname -r": "5.10.0\n",
        "uname -m": "x86_64\n",
        "hostname -f || hostname": "vm.corp\n",
        "systemctl list-units --type=service --state=running --no-legend --no-pager --plain": "",
        "ip -o -4 addr show": "",
        "ip -o -6 addr show": "",
        "ip -4 route show": "",
        "cat /etc/resolv.conf": "",
        "ss -tulnH": "",
        "findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,USED": "",
        "cut -d: -f1 /etc/passwd": "",
        (
            "find /etc/cron.d /etc/crontab /etc/cron.hourly /etc/cron.daily "
            "/etc/cron.weekly /etc/cron.monthly -maxdepth 1 -type f 2>/dev/null"
        ): "",
    }
    stub = _StubClient(responses)
    stub.exec_command = lambda cmd, timeout=0: _exec(stub, cmd, timeout)

    from contextlib import contextmanager

    @contextmanager
    def fake_connect(self, host, username):
        yield stub

    monkeypatch.setattr(SSHCollector, "_connect", fake_connect)

    collector = SSHCollector(key_path="/dev/null")
    state = collector.collect(host="10.0.0.5", username="virtvalidate")

    # Unknown distro → low confidence, modern defaults.
    profile = state["meta"]["os_profile"]
    assert profile["distro"] == "unknown"
    assert profile["detection_confidence"] == "low"
    assert "ss -tulnH" in stub.commands
