"""
Per-OS command dispatch for the SSH collector.

Each ``CommandSet`` is a frozen bundle of the exact shell commands the
collector should run for a given target OS. The collector never writes a
literal command string — it always reaches through a ``CommandSet`` so a
single dispatch table at the bottom of this file is the only place that
needs to know about RHEL 7 vs RHEL 9 vs Ubuntu differences.

Today the collector only runs the "currently collected" commands at the
top of the dataclass. The "anticipated future" fields (firewall, time
sync, package list) are populated for every OS so wiring new collection
in later is a matter of calling ``cs.<field>`` from the collector — no
changes to the dispatch table.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.os_profile import OSProfile


@dataclass(frozen=True)
class CommandSet:
    """The shell commands to run on a given OS.

    Attribute names are normalized across all OS variants — only the
    underlying command string changes per dispatch entry. This keeps the
    collector free of ``if rhel else ...`` branching.
    """

    # ---- currently collected ----
    services_running: str
    network_addr_v4: str
    network_addr_v6: str
    network_routes: str
    resolv_conf: str
    listening_ports: str
    mounts: str
    cron_users: str
    cron_user_template: str  # contains {user} — caller substitutes
    cron_system_paths: str
    os_release: str
    uname_kernel: str
    uname_arch: str
    hostname_fqdn: str

    # ---- anticipated future collection (not yet wired into SSHCollector,
    # but populated for every OS so adding it later doesn't fork code) ----
    firewall_inspect: str
    time_sync_status: str
    package_list: str


# ---------------------------------------------------------------------------
# Command snippets that don't change across the OS matrix.
#
# Pulled out into module-level constants so the dispatch table reads as a
# delta from a baseline rather than three near-duplicate copies.
# ---------------------------------------------------------------------------
_NETWORK_ADDR_V4 = "ip -o -4 addr show"
_NETWORK_ADDR_V6 = "ip -o -6 addr show"
_NETWORK_ROUTES = "ip -4 route show"
_RESOLV_CONF = "cat /etc/resolv.conf"
_MOUNTS = "findmnt -rn -o TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,USED"
_CRON_USERS = "cut -d: -f1 /etc/passwd"
_CRON_USER_TEMPLATE = "crontab -l -u {user} 2>/dev/null"
_CRON_SYSTEM_PATHS = (
    "find /etc/cron.d /etc/crontab /etc/cron.hourly /etc/cron.daily "
    "/etc/cron.weekly /etc/cron.monthly -maxdepth 1 -type f 2>/dev/null"
)
_OS_RELEASE = "cat /etc/os-release"
_UNAME_KERNEL = "uname -r"
_UNAME_ARCH = "uname -m"
_HOSTNAME = "hostname -f || hostname"
_SERVICES_SYSTEMD = (
    "systemctl list-units --type=service --state=running " "--no-legend --no-pager --plain"
)


# ---------------------------------------------------------------------------
# Per-OS dispatch.
#
# Where commands diverge:
#
#   RHEL 7 era:
#     - ss(8) from iproute-3.10 doesn't support `-H`, so we strip the
#       header line ourselves with `tail -n +2`.
#     - Firewall is iptables; nftables isn't shipped.
#     - Package manager is yum.
#     - Time sync is ntpd by default (chronyd is opt-in).
#
#   RHEL 8/9/10 + Rocky/Alma/Fedora:
#     - ss(8) supports `-H` for headerless output.
#     - Firewall pivoted to nftables (iptables is a compat wrapper).
#     - dnf replaced yum (yum is symlinked to dnf for compat).
#     - chronyd is the default; ntpd is removed.
#
#   Debian/Ubuntu (planned, listed for completeness):
#     - ss supports `-H`.
#     - ufw front-ends iptables/nftables, but the canonical inspect for
#       us is still nftables.
#     - apt replaces yum/dnf.
# ---------------------------------------------------------------------------
def _rhel7() -> CommandSet:
    return CommandSet(
        services_running=_SERVICES_SYSTEMD,
        network_addr_v4=_NETWORK_ADDR_V4,
        network_addr_v6=_NETWORK_ADDR_V6,
        network_routes=_NETWORK_ROUTES,
        resolv_conf=_RESOLV_CONF,
        # `-H` is unavailable; drop the header line manually.
        listening_ports="ss -tuln | tail -n +2",
        mounts=_MOUNTS,
        cron_users=_CRON_USERS,
        cron_user_template=_CRON_USER_TEMPLATE,
        cron_system_paths=_CRON_SYSTEM_PATHS,
        os_release=_OS_RELEASE,
        uname_kernel=_UNAME_KERNEL,
        uname_arch=_UNAME_ARCH,
        hostname_fqdn=_HOSTNAME,
        firewall_inspect="iptables -L -n -v 2>/dev/null",
        time_sync_status="ntpq -pn 2>/dev/null || chronyc sources 2>/dev/null",
        package_list="yum list installed 2>/dev/null | tail -n +2",
    )


def _rhel_modern() -> CommandSet:
    """RHEL 8 / 9 / 10 / Rocky / Alma / Fedora 39+."""
    return CommandSet(
        services_running=_SERVICES_SYSTEMD,
        network_addr_v4=_NETWORK_ADDR_V4,
        network_addr_v6=_NETWORK_ADDR_V6,
        network_routes=_NETWORK_ROUTES,
        resolv_conf=_RESOLV_CONF,
        listening_ports="ss -tulnH",
        mounts=_MOUNTS,
        cron_users=_CRON_USERS,
        cron_user_template=_CRON_USER_TEMPLATE,
        cron_system_paths=_CRON_SYSTEM_PATHS,
        os_release=_OS_RELEASE,
        uname_kernel=_UNAME_KERNEL,
        uname_arch=_UNAME_ARCH,
        hostname_fqdn=_HOSTNAME,
        firewall_inspect="nft list ruleset 2>/dev/null || iptables -L -n -v 2>/dev/null",
        time_sync_status="chronyc sources 2>/dev/null",
        package_list="dnf list --installed 2>/dev/null | tail -n +2",
    )


def _debian_like() -> CommandSet:
    """Ubuntu / Debian. Wired up for forward compatibility — see the
    "Future" rows in docs/COMPATIBILITY.md; we don't support running
    against these in production yet."""
    return CommandSet(
        services_running=_SERVICES_SYSTEMD,
        network_addr_v4=_NETWORK_ADDR_V4,
        network_addr_v6=_NETWORK_ADDR_V6,
        network_routes=_NETWORK_ROUTES,
        resolv_conf=_RESOLV_CONF,
        listening_ports="ss -tulnH",
        mounts=_MOUNTS,
        cron_users=_CRON_USERS,
        cron_user_template=_CRON_USER_TEMPLATE,
        cron_system_paths=_CRON_SYSTEM_PATHS,
        os_release=_OS_RELEASE,
        uname_kernel=_UNAME_KERNEL,
        uname_arch=_UNAME_ARCH,
        hostname_fqdn=_HOSTNAME,
        firewall_inspect=(
            "nft list ruleset 2>/dev/null "
            "|| ufw status verbose 2>/dev/null "
            "|| iptables -L -n -v 2>/dev/null"
        ),
        time_sync_status=(
            "timedatectl show-timesync 2>/dev/null " "|| chronyc sources 2>/dev/null"
        ),
        package_list="dpkg-query -W -f='${Package}\\t${Version}\\n' 2>/dev/null",
    )


def _modern_default() -> CommandSet:
    """Conservative default for unknown distros — assumes systemd, ip,
    and ss with `-H`. Logged at "low" detection confidence so operators
    know they're in best-effort territory."""
    return _rhel_modern()


def command_set_for(profile: OSProfile) -> CommandSet:
    """Pick the right ``CommandSet`` for an ``OSProfile``.

    Branching rules — keep them coarse so future OS additions don't
    explode the matrix:

      - RHEL-like, major <= 7   → RHEL 7 commandset (legacy iproute, yum)
      - RHEL-like, major >= 8   → RHEL modern commandset
      - Debian-like             → Debian/Ubuntu commandset
      - Anything else (unknown) → conservative modern-Linux defaults
    """
    if profile.distro_family == "rhel-like":
        if profile.major_version and profile.major_version <= 7:
            return _rhel7()
        return _rhel_modern()
    if profile.distro_family == "debian-like":
        return _debian_like()
    return _modern_default()
