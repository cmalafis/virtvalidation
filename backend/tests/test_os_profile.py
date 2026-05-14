"""Tests for OS detection + per-OS command dispatch.

Sample ``/etc/os-release`` payloads are real ones from upstream — kept in
``OS_RELEASE_*`` constants below so it's obvious which strings the
collector is supposed to handle.
"""

from __future__ import annotations

import pytest

from app.core.commands import CommandSet, command_set_for
from app.core.os_profile import UNKNOWN_PROFILE, detect, detect_windows

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
# Windows detection — Get-CimInstance Win32_OperatingSystem JSON
# ---------------------------------------------------------------------------

# Real Get-CimInstance Win32_OperatingSystem | ConvertTo-Json output, trimmed
# to the fields the detector reads. The full payload includes ~40 properties;
# we keep only what the detector branches on so the fixtures stay readable.
WIN_2019_CIM_JSON = """
{
  "Caption": "Microsoft Windows Server 2019 Datacenter",
  "Version": "10.0.17763",
  "BuildNumber": "17763",
  "OSArchitecture": "64-bit",
  "OSType": 18
}
"""

WIN_2022_CIM_JSON = """
{
  "Caption": "Microsoft Windows Server 2022 Standard",
  "Version": "10.0.20348",
  "BuildNumber": "20348",
  "OSArchitecture": "64-bit",
  "OSType": 18
}
"""

WIN_2025_CIM_JSON = """
{
  "Caption": "Microsoft Windows Server 2025 Datacenter",
  "Version": "10.0.26100",
  "BuildNumber": "26100",
  "OSArchitecture": "64-bit",
  "OSType": 18
}
"""

# Get-CimInstance can return a JSON array when the input pipeline produces
# multiple objects — defensive case the detector should handle by picking
# the first element.
WIN_2022_CIM_ARRAY = '[{"Caption":"Microsoft Windows Server 2022 Standard","Version":"10.0.20348","BuildNumber":"20348","OSArchitecture":"64-bit"}]'

WIN_UNKNOWN_BUILD_JSON = """
{
  "Caption": "Microsoft Windows Server vNext Preview",
  "Version": "10.0.99999",
  "BuildNumber": "99999",
  "OSArchitecture": "ARM64"
}
"""


def test_detect_windows_server_2019():
    profile = detect_windows(get_ciminstance_json=WIN_2019_CIM_JSON)
    assert profile.distro == "windows-server-2019"
    assert profile.distro_family == "windows"
    assert profile.major_version == 10
    assert profile.minor_version == 17763
    assert profile.kernel_version == "10.0.17763"
    assert profile.architecture == "64-bit"
    assert profile.is_systemd is False
    assert profile.detection_confidence == "high"
    assert "Server 2019" in profile.pretty_name


def test_detect_windows_server_2022():
    profile = detect_windows(get_ciminstance_json=WIN_2022_CIM_JSON)
    assert profile.distro == "windows-server-2022"
    assert profile.distro_family == "windows"
    assert profile.minor_version == 20348
    assert profile.detection_confidence == "high"


def test_detect_windows_server_2025():
    profile = detect_windows(get_ciminstance_json=WIN_2025_CIM_JSON)
    assert profile.distro == "windows-server-2025"
    assert profile.minor_version == 26100
    assert profile.detection_confidence == "high"


def test_detect_windows_handles_top_level_array():
    profile = detect_windows(get_ciminstance_json=WIN_2022_CIM_ARRAY)
    # Should pick the first element transparently.
    assert profile.distro == "windows-server-2022"


def test_detect_windows_unknown_build_falls_back_to_windows_unknown():
    """Unknown build numbers (preview channels, future releases) keep the
    family routing to Windows so the PowerShell command set still
    dispatches — just at lower confidence."""
    profile = detect_windows(get_ciminstance_json=WIN_UNKNOWN_BUILD_JSON)
    assert profile.distro == "windows-unknown"
    assert profile.distro_family == "windows"
    assert profile.detection_confidence == "medium"


def test_detect_windows_handles_malformed_json():
    """Don't crash on garbage input — return windows-unknown so the
    collector can keep going (or short-circuit cleanly)."""
    profile = detect_windows(get_ciminstance_json="{not valid json")
    assert profile.distro == "windows-unknown"
    assert profile.distro_family == "windows"
    assert profile.detection_confidence == "low"


def test_detect_windows_handles_empty_input():
    profile = detect_windows(get_ciminstance_json="")
    assert profile.distro == "windows-unknown"
    assert profile.distro_family == "windows"


def test_command_set_for_windows_returns_powershell_commands():
    profile = detect_windows(get_ciminstance_json=WIN_2022_CIM_JSON)
    cs = command_set_for(profile)
    # Every required field is a PowerShell invocation. We don't try to
    # match the full string — just confirm the command set didn't fall
    # through to the Linux dispatch.
    assert "powershell" in cs.services_running.lower()
    assert "powershell" in cs.listening_ports.lower()
    assert "powershell" in cs.mounts.lower()
    # Windows-only fields populated.
    assert cs.windows_hotfixes is not None
    assert "Get-HotFix" in cs.windows_hotfixes
    assert cs.windows_ad_membership is not None


def test_command_set_for_windows_uses_convert_to_json():
    """Every Windows command emits structured JSON the collector can
    parse without text scraping."""
    profile = detect_windows(get_ciminstance_json=WIN_2022_CIM_JSON)
    cs = command_set_for(profile)
    for cmd in (
        cs.services_running,
        cs.listening_ports,
        cs.mounts,
        cs.network_addr_v4,
        cs.network_addr_v6,
        cs.network_routes,
        cs.cron_system_paths,
    ):
        assert "ConvertTo-Json" in cmd, f"Missing ConvertTo-Json in: {cmd}"


def test_linux_command_set_does_not_populate_windows_fields():
    """Sanity — the Linux dispatch leaves the Windows-only fields None
    so existing Linux call sites don't have to know about them."""
    cs = _cs(OS_RELEASE_RHEL_9)
    assert cs.windows_hotfixes is None
    assert cs.windows_ad_membership is None
    assert cs.windows_processes is None


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


# ---------------------------------------------------------------------------
# Windows path — collector falls through POSIX detection, runs PowerShell,
# normalizes JSON output to the Linux-shaped raw_data fields.
# ---------------------------------------------------------------------------
class _StubStdoutBytes:
    """Variant of _StubStdout whose ``read()`` returns raw bytes —
    lets a test exercise the UTF-16 LE BOM path that real PowerShell
    sessions hit on Server 2019 hosts."""

    def __init__(self, raw: bytes):
        self._raw = raw
        self.channel = _StubChannel(0)

    def read(self) -> bytes:
        return self._raw


def _win_fixture_responses() -> dict[str, bytes]:
    """Build a full set of canned PowerShell responses keyed by the
    exact command string the Windows CommandSet emits. Returned as
    raw bytes so the BOM and CRLF behavior is realistic."""
    import json as _json

    # Helper — encode JSON as UTF-16 LE with BOM, with CRLF line endings.
    # Mimics what real Server 2019 OpenSSH+PowerShell emits.
    def utf16(payload) -> bytes:
        body = _json.dumps(payload) + "\r\n"
        return b"\xff\xfe" + body.encode("utf-16-le")

    def utf8(payload) -> bytes:
        return (_json.dumps(payload) + "\r\n").encode("utf-8")

    # Build the same command strings the Windows CommandSet emits so the
    # stub matches the collector's call sites verbatim. Pull them from
    # the live CommandSet so this test stays in sync if the strings
    # change.
    profile = detect_windows(get_ciminstance_json=WIN_2022_CIM_JSON)
    cs = command_set_for(profile)

    return {
        # OS detection — POSIX probe fails (empty), Windows probe succeeds.
        "cat /etc/os-release": b"",
        # The detector's hard-coded Windows probe.
        (
            'powershell -NoProfile -NonInteractive -Command "'
            "Get-CimInstance Win32_OperatingSystem | "
            'ConvertTo-Json -Compress -Depth 3"'
        ): utf16(_json.loads(WIN_2022_CIM_JSON)),
        cs.hostname_fqdn: b"\xff\xfeW\x00I\x00N\x002\x002\x00.\x00c\x00o\x00r\x00p\x00\r\x00\n\x00",
        cs.services_running: utf16(
            [
                {
                    "Name": "sshd",
                    "Status": "Running",
                    "StartType": "Automatic",
                    "DisplayName": "OpenSSH SSH Server",
                },
                {
                    "Name": "W3SVC",
                    "Status": "Running",
                    "StartType": "Automatic",
                    "DisplayName": "World Wide Web Publishing Service",
                },
            ]
        ),
        cs.network_addr_v4: utf8(
            [{"InterfaceAlias": "Ethernet0", "IPAddress": "10.0.0.5", "PrefixLength": 24}]
        ),
        cs.network_addr_v6: utf8([]),
        cs.network_routes: utf8(
            [
                {
                    "DestinationPrefix": "0.0.0.0/0",
                    "NextHop": "10.0.0.1",
                    "InterfaceAlias": "Ethernet0",
                    "RouteMetric": 0,
                }
            ]
        ),
        cs.resolv_conf: utf8(
            [{"InterfaceAlias": "Ethernet0", "ServerAddresses": ["10.0.0.2", "10.0.0.3"]}]
        ),
        cs.listening_ports: utf8(
            [
                {"LocalAddress": "0.0.0.0", "LocalPort": 22, "OwningProcess": 1234},
                {"LocalAddress": "0.0.0.0", "LocalPort": 80, "OwningProcess": 5678},
            ]
        ),
        cs.mounts: utf8(
            [
                {
                    "DriveLetter": "C",
                    "FileSystemLabel": "OS",
                    "FileSystemType": "NTFS",
                    "Size": 64424509440,
                    "SizeRemaining": 32212254720,
                    "HealthStatus": "Healthy",
                }
            ]
        ),
        cs.cron_system_paths: utf8(
            [
                {"TaskName": "Backup", "TaskPath": "\\Custom\\", "State": "Ready", "Author": "ops"},
                {"TaskName": "Patch", "TaskPath": "\\Custom\\", "State": "Ready", "Author": "ops"},
            ]
        ),
        cs.windows_hotfixes: utf8(
            [
                {
                    "HotFixID": "KB5036909",
                    "Description": "Security Update",
                    "InstalledOn": "2026-04-01",
                }
            ]
        ),
        cs.windows_ad_membership: utf8(
            {"Domain": "corp.local", "PartOfDomain": True, "DomainRole": 3}
        ),
    }


def test_collector_drives_windows_path_and_normalizes_to_linux_shape(monkeypatch):
    """End-to-end: SSHCollector talks to a Windows VM, parses
    PowerShell+JSON output (with UTF-16 BOM on some commands), and
    produces raw_data in the same shape the diff engine consumes for
    Linux VMs."""
    from app.core.ssh import SSHCollector

    responses = _win_fixture_responses()
    stub = _StubClient({})  # we override exec_command directly

    def _exec_bytes(cmd, timeout=0):
        stub.commands.append(cmd)
        body = responses.get(cmd, b"")
        return None, _StubStdoutBytes(body), _StubStdoutBytes(b"")

    stub.exec_command = _exec_bytes

    from contextlib import contextmanager

    @contextmanager
    def fake_connect(self, host, username):
        yield stub

    monkeypatch.setattr(SSHCollector, "_connect", fake_connect)

    collector = SSHCollector(key_path="/dev/null")
    state = collector.collect(host="10.0.0.5", username="virtvalidate")

    # OS profile picked Windows.
    profile = state["meta"]["os_profile"]
    assert profile["distro"] == "windows-server-2022"
    assert profile["distro_family"] == "windows"
    assert profile["is_systemd"] is False

    # POSIX probe ran first, came back empty, then Windows probe fired.
    assert stub.commands[0] == "cat /etc/os-release"
    assert any("Win32_OperatingSystem" in c for c in stub.commands)

    # Services normalized to systemd-shaped fields the diff engine reads.
    services = state["services"]
    units = {s["unit"] for s in services}
    assert {"sshd", "W3SVC"} <= units
    assert all(s["active"] == "active" for s in services)

    # Ports look the same as Linux ones.
    ports = state["ports"]
    assert {"proto": "tcp", "state": "LISTEN", "address": "0.0.0.0", "port": 22} in ports
    assert any(p["port"] == 80 for p in ports)

    # Volumes mapped to the mounts field.
    mounts = state["mounts"]
    assert any("C:" in m["target"] for m in mounts)
    assert mounts[0]["fstype"] == "NTFS"

    # Network normalized.
    net = state["network"]
    assert "Ethernet0" in net["interfaces"]
    assert "10.0.0.5/24" in net["interfaces"]["Ethernet0"]["ipv4"]
    assert "10.0.0.2" in net["dns"]

    # Scheduled tasks land under cron.system so the diff engine compares
    # them the same way it compares /etc/cron.d/* on Linux.
    cron = state["cron"]
    assert cron["user_crontabs"] == {}
    paths = {entry["path"] for entry in cron["system"]}
    assert "\\Custom\\" in paths

    # Windows-only fields populated alongside the shared ones.
    assert state["hotfixes"][0]["id"] == "KB5036909"
    assert state["ad_membership"]["domain"] == "corp.local"
    assert state["ad_membership"]["part_of_domain"] is True


def test_run_strips_utf16_bom_and_crlf():
    """Direct unit test for _run — confirms UTF-16 LE and UTF-8 BOM
    handling without going through the full collect() path."""
    from app.core.ssh import SSHCollector

    collector = SSHCollector(key_path="/dev/null")

    class _Client:
        def exec_command(self, cmd, timeout=0):
            # UTF-16 LE BOM + "ok\r\n"
            return None, _StubStdoutBytes(b"\xff\xfeo\x00k\x00\r\x00\n\x00"), _StubStdoutBytes(b"")

    out = collector._run(_Client(), "any-command")
    assert out == "ok\n"


def test_run_strips_utf8_bom():
    from app.core.ssh import SSHCollector

    collector = SSHCollector(key_path="/dev/null")

    class _Client:
        def exec_command(self, cmd, timeout=0):
            return None, _StubStdoutBytes(b"\xef\xbb\xbfhello\r\n"), _StubStdoutBytes(b"")

    assert collector._run(_Client(), "any") == "hello\n"


def test_run_json_returns_none_on_garbage():
    from app.core.ssh import SSHCollector

    collector = SSHCollector(key_path="/dev/null")

    class _Client:
        def exec_command(self, cmd, timeout=0):
            return None, _StubStdoutBytes(b"not valid json\r\n"), _StubStdoutBytes(b"")

    assert collector._run_json(_Client(), "any") is None
