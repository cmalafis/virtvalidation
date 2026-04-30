"""Unit tests for app.core.ssh — parsers and connection error handling.

Per-OS dispatch is exercised in tests/test_os_profile.py. The tests here
just verify the collector method bodies parse the canned outputs correctly
when handed an explicit CommandSet.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.core.commands import command_set_for
from app.core.os_profile import detect
from app.core.ssh import SSHCollectionError, SSHCollector


@pytest.fixture
def rhel9_cs():
    """The CommandSet a collector running against a RHEL 9 target would pick."""
    profile = detect(
        os_release_text='ID="rhel"\nVERSION_ID="9.4"\nPRETTY_NAME="RHEL 9.4"\n',
        uname_r="5.14.0",
        uname_m="x86_64",
    )
    return command_set_for(profile)


def test_collect_services_parses_systemctl_output(rhel9_cs):
    collector = SSHCollector()
    output = (
        "sshd.service           loaded active running OpenSSH server daemon\n"
        "postgresql.service     loaded active running PostgreSQL database\n"
    )
    with patch.object(collector, "_run", return_value=output):
        services = collector._collect_services(MagicMock(), rhel9_cs)
    assert [s["unit"] for s in services] == ["sshd.service", "postgresql.service"]
    assert services[0]["active"] == "active"
    assert services[0]["description"] == "OpenSSH server daemon"
    assert services[1]["sub"] == "running"


def test_collect_services_skips_truncated_lines(rhel9_cs):
    collector = SSHCollector()
    # Only 3 fields — below the 4-field minimum
    with patch.object(collector, "_run", return_value="short line\n"):
        services = collector._collect_services(MagicMock(), rhel9_cs)
    assert services == []


def test_collect_ports_parses_ss_output(rhel9_cs):
    collector = SSHCollector()
    output = (
        "tcp  LISTEN 0  128  0.0.0.0:22    0.0.0.0:*\n"
        "udp  UNCONN 0  0    127.0.0.1:53  0.0.0.0:*\n"
    )
    with patch.object(collector, "_run", return_value=output):
        ports = collector._collect_ports(MagicMock(), rhel9_cs)
    assert {"proto": "tcp", "state": "LISTEN", "address": "0.0.0.0", "port": 22} in ports
    assert {"proto": "udp", "state": "UNCONN", "address": "127.0.0.1", "port": 53} in ports


def test_collect_network_builds_interfaces_routes_and_dns(rhel9_cs):
    collector = SSHCollector()
    responses = {
        rhel9_cs.network_addr_v4: "2: eth0    inet 10.0.0.5/24 brd 10.0.0.255 scope global\n",
        rhel9_cs.network_addr_v6: "2: eth0    inet6 fe80::1/64 scope link\n",
        rhel9_cs.network_routes: "default via 10.0.0.1 dev eth0\n10.0.0.0/24 dev eth0\n",
        rhel9_cs.resolv_conf: "nameserver 8.8.8.8\nnameserver 1.1.1.1\n",
    }

    def fake_run(_client, cmd):
        return responses.get(cmd, "")

    with patch.object(collector, "_run", side_effect=fake_run):
        network = collector._collect_network(MagicMock(), rhel9_cs)

    assert network["interfaces"]["eth0"]["ipv4"] == ["10.0.0.5/24"]
    assert network["interfaces"]["eth0"]["ipv6"] == ["fe80::1/64"]
    assert "default via 10.0.0.1 dev eth0" in network["routes"]
    assert network["dns"] == ["8.8.8.8", "1.1.1.1"]


def test_collect_mounts_filters_pseudo_filesystems(rhel9_cs):
    collector = SSHCollector()
    output = (
        "/         /dev/sda1  xfs   rw\n"
        "/proc     proc       proc  rw\n"
        "/sys      sysfs      sysfs rw\n"
        "/home     /dev/sdb1  xfs   rw\n"
    )
    with patch.object(collector, "_run", return_value=output):
        mounts = collector._collect_mounts(MagicMock(), rhel9_cs)
    targets = [m["target"] for m in mounts]
    assert "/" in targets
    assert "/home" in targets
    assert "/proc" not in targets
    assert "/sys" not in targets


def test_collect_cron_captures_user_and_system_entries(rhel9_cs):
    collector = SSHCollector()
    responses = {
        rhel9_cs.cron_users: "root\nvirtvalidate\n",
        "crontab -l -u root 2>/dev/null": "0 3 * * * /usr/bin/backup.sh\n# comment\n",
        "crontab -l -u virtvalidate 2>/dev/null": "",
        rhel9_cs.cron_system_paths: "/etc/cron.d/audit\n",
        "cat /etc/cron.d/audit": "*/15 * * * * root /opt/audit.sh\n",
    }

    def fake_run(_client, cmd):
        return responses.get(cmd, "")

    with patch.object(collector, "_run", side_effect=fake_run):
        cron = collector._collect_cron(MagicMock(), rhel9_cs)

    assert cron["user_crontabs"] == {"root": ["0 3 * * * /usr/bin/backup.sh"]}
    assert len(cron["system"]) == 1
    assert cron["system"][0]["path"] == "/etc/cron.d/audit"


def test_connect_raises_when_key_missing(tmp_path):
    collector = SSHCollector(key_path=str(tmp_path / "does-not-exist"))
    with pytest.raises(SSHCollectionError, match="SSH key not found"):
        with collector._connect("host", "user"):
            pass


def test_collect_builds_full_state_dict(mock_ssh_state):
    """Smoke test for collect() with _connect patched.

    Validates the full pipeline: OS detection on the wire → CommandSet
    dispatch → per-subsystem parsing → meta block carries os_profile.
    """
    collector = SSHCollector()
    mock_client = MagicMock()

    responses = {
        "cat /etc/os-release": 'ID="rhel"\nVERSION_ID="9.2"\nPRETTY_NAME="RHEL 9.2"\n',
        "uname -r": "5.14.0-362\n",
        "uname -m": "x86_64\n",
        "hostname -f || hostname": "db-01.corp\n",
        "systemctl list-units --type=service --state=running "
        "--no-legend --no-pager --plain": "sshd.service loaded active running OpenSSH\n",
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

    def fake_run(_client, cmd):
        return responses.get(cmd, "")

    with (
        patch.object(collector, "_connect") as mock_connect,
        patch.object(collector, "_run", side_effect=fake_run),
    ):
        mock_connect.return_value.__enter__.return_value = mock_client
        mock_connect.return_value.__exit__.return_value = False
        state = collector.collect(host="10.0.0.5", username="virtvalidate")

    assert state["meta"]["hostname"] == "db-01.corp"
    assert state["meta"]["kernel"] == "5.14.0-362"
    # Backwards-compat block still populated.
    assert state["meta"]["os"]["id"] == "rhel"
    # New structured profile is the source of truth.
    assert state["meta"]["os_profile"]["distro"] == "rhel"
    assert state["meta"]["os_profile"]["major_version"] == 9
    assert state["meta"]["os_profile"]["detection_confidence"] == "high"
    assert state["meta"]["host"] == "10.0.0.5"
    assert "services" in state
    assert "network" in state
