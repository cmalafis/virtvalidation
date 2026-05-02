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

    Windows fields default to ``None`` so the existing Linux command sets
    don't have to populate them. The collector consults
    ``profile.distro_family`` to decide which path to drive.
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

    # ---- Windows-only fields (None on Linux command sets). The collector
    # reads these only when ``profile.distro_family == "windows"``; on
    # Linux it stays on the POSIX fields above. The values are full
    # PowerShell command strings the collector pipes through SSH.
    windows_system_info: str | None = None
    windows_hotfixes: str | None = None
    windows_ad_membership: str | None = None
    windows_processes: str | None = None
    windows_users: str | None = None


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


# ---------------------------------------------------------------------------
# Windows Server (2019 / 2022 / 2025).
#
# All commands run via PowerShell over OpenSSH. ConvertTo-Json gives us
# structured output the collector parses directly — no fragile text scraping.
# Compress reduces newlines + indentation that would otherwise have to
# survive an SSH channel cleanly. The wrapping `powershell -Command "…"`
# is required because Windows OpenSSH defaults to CMD when the operator
# hasn't set DefaultShell to PowerShell.
#
# The Linux-shaped fields (services_running, listening_ports, mounts,
# cron_*) are populated with PowerShell equivalents so the collector's
# main loop walks the same fields regardless of OS — the parsing branch
# decides whether to scrape text or json.loads().
# ---------------------------------------------------------------------------
_PS = "powershell -NoProfile -NonInteractive -Command"


def _ps(cmd: str) -> str:
    """Wrap a PowerShell expression so it survives the SSH channel.

    Doubles up double quotes inside the command body — the outer SSH
    layer hands the entire string to the remote shell as one argv,
    which on Windows means the command body must already be quoted
    correctly. Single quotes inside PowerShell strings stay single.
    """
    body = cmd.strip()
    return f'{_PS} "{body}"'


def _windows_powershell() -> CommandSet:
    """PowerShell-based commands for Windows Server 2019+.

    Maps the Linux-shaped CommandSet fields to their Windows equivalents:

      - services_running    → Get-Service (running only)
      - listening_ports     → Get-NetTCPConnection -State Listen
      - mounts              → Get-Volume (drives + filesystems)
      - cron_*              → Get-ScheduledTask (cron_user_template
                              re-purposed as a single-shot command —
                              the {user} substitution is ignored)
      - network_addr_v4/v6  → Get-NetIPAddress -AddressFamily IPv4/IPv6
      - network_routes      → Get-NetRoute -AddressFamily IPv4
      - resolv_conf         → Get-DnsClientServerAddress
      - os_release          → Get-CimInstance Win32_OperatingSystem
      - uname_kernel/arch   → derived from Win32_OperatingSystem
      - hostname_fqdn       → [System.Net.Dns]::GetHostByName output
      - firewall_inspect    → Get-NetFirewallRule (enabled rules)
      - time_sync_status    → w32tm /query /status
      - package_list        → Get-CimInstance Win32_Product

    All commands emit JSON via ConvertTo-Json. The SSH collector
    branches on ``profile.distro_family == "windows"`` and parses
    accordingly.
    """
    return CommandSet(
        services_running=_ps(
            "Get-Service | Where-Object { $_.Status -eq 'Running' } | "
            "Select-Object Name, Status, StartType, DisplayName | "
            "ConvertTo-Json -Compress"
        ),
        network_addr_v4=_ps(
            "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
            "Select-Object InterfaceAlias, IPAddress, PrefixLength | "
            "ConvertTo-Json -Compress"
        ),
        network_addr_v6=_ps(
            "Get-NetIPAddress -AddressFamily IPv6 -ErrorAction SilentlyContinue | "
            "Select-Object InterfaceAlias, IPAddress, PrefixLength | "
            "ConvertTo-Json -Compress"
        ),
        network_routes=_ps(
            "Get-NetRoute -AddressFamily IPv4 | "
            "Select-Object DestinationPrefix, NextHop, InterfaceAlias, RouteMetric | "
            "ConvertTo-Json -Compress"
        ),
        resolv_conf=_ps(
            "Get-DnsClientServerAddress -AddressFamily IPv4 | "
            "Select-Object InterfaceAlias, ServerAddresses | "
            "ConvertTo-Json -Compress"
        ),
        listening_ports=_ps(
            "Get-NetTCPConnection -State Listen | "
            "Select-Object LocalAddress, LocalPort, OwningProcess | "
            "ConvertTo-Json -Compress"
        ),
        mounts=_ps(
            "Get-Volume | "
            "Select-Object DriveLetter, FileSystemLabel, FileSystemType, "
            "Size, SizeRemaining, HealthStatus | "
            "ConvertTo-Json -Compress"
        ),
        # Windows has scheduled tasks instead of crontabs. The collector
        # keys on the same fields (cron_users / cron_user_template /
        # cron_system_paths) but here cron_users returns nothing (so the
        # collector skips per-user iteration) and cron_system_paths
        # returns the full task list as JSON.
        cron_users="",
        cron_user_template="",
        cron_system_paths=_ps(
            "Get-ScheduledTask | Where-Object { $_.State -eq 'Ready' } | "
            "Select-Object TaskName, TaskPath, State, Author | "
            "ConvertTo-Json -Compress"
        ),
        os_release=_ps(
            "Get-CimInstance Win32_OperatingSystem | "
            "ConvertTo-Json -Compress -Depth 3"
        ),
        # uname_kernel + uname_arch are emitted from the same Win32 query
        # so we don't need three round-trips for OS detection. The
        # collector pulls them from the os_release JSON itself.
        uname_kernel="",
        uname_arch="",
        hostname_fqdn=_ps(
            "[System.Net.Dns]::GetHostEntry($env:COMPUTERNAME).HostName"
        ),
        firewall_inspect=_ps(
            "Get-NetFirewallRule | Where-Object Enabled -eq 'True' | "
            "Select-Object DisplayName, Direction, Action, Profile | "
            "ConvertTo-Json -Compress"
        ),
        time_sync_status="w32tm /query /status",
        package_list=_ps(
            "Get-CimInstance Win32_Product | "
            "Select-Object Name, Version, Vendor | "
            "ConvertTo-Json -Compress"
        ),
        windows_system_info=_ps(
            "Get-CimInstance Win32_OperatingSystem | "
            "ConvertTo-Json -Compress -Depth 3"
        ),
        windows_hotfixes=_ps(
            "Get-HotFix | "
            "Select-Object HotFixID, Description, InstalledOn | "
            "ConvertTo-Json -Compress"
        ),
        windows_ad_membership=_ps(
            "Get-CimInstance Win32_ComputerSystem | "
            "Select-Object Domain, PartOfDomain, DomainRole | "
            "ConvertTo-Json -Compress"
        ),
        windows_processes=_ps(
            "Get-Process | Sort-Object CPU -Descending | "
            "Select-Object -First 20 ProcessName, Id, CPU, WorkingSet | "
            "ConvertTo-Json -Compress"
        ),
        windows_users=_ps(
            "Get-LocalUser | "
            "Select-Object Name, Enabled, LastLogon | "
            "ConvertTo-Json -Compress"
        ),
    )


def command_set_for(profile: OSProfile) -> CommandSet:
    """Pick the right ``CommandSet`` for an ``OSProfile``.

    Branching rules — keep them coarse so future OS additions don't
    explode the matrix:

      - Windows                 → PowerShell + ConvertTo-Json command set
      - RHEL-like, major <= 7   → RHEL 7 commandset (legacy iproute, yum)
      - RHEL-like, major >= 8   → RHEL modern commandset
      - Debian-like             → Debian/Ubuntu commandset
      - Anything else (unknown) → conservative modern-Linux defaults
    """
    if profile.distro_family == "windows":
        return _windows_powershell()
    if profile.distro_family == "rhel-like":
        if profile.major_version and profile.major_version <= 7:
            return _rhel7()
        return _rhel_modern()
    if profile.distro_family == "debian-like":
        return _debian_like()
    return _modern_default()
