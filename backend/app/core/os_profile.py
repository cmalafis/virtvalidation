"""
OS detection for the SSH collector.

Runs as the first step of every collection so the rest of the routine can
branch on what it found. Output goes into the snapshot's ``meta.os_profile``
block — both the LLM and the dashboard's VM detail view read from there.

Detection is best-effort. When ``/etc/os-release`` is unparseable or the
distro is unrecognized, ``OSProfile`` records that explicitly via
``distro="unknown"`` + ``detection_confidence="low"`` rather than crashing
or guessing — see ``docs/COMPATIBILITY.md`` for the full matrix.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

DistroName = Literal[
    "rhel",
    "fedora",
    "rocky",
    "alma",
    "centos",
    "ubuntu",
    "debian",
    # Windows Server family — same OSProfile shape, different command set.
    # Build numbers map cleanly to product names: 17763=2019, 20348=2022,
    # 26100=2025; we surface the product name as distro and the build as
    # minor_version so existing UI rendering (`distro <major>.<minor>`)
    # produces a sensible "windows-server-2022 10.20348" string.
    "windows-server-2019",
    "windows-server-2022",
    "windows-server-2025",
    "windows-unknown",
    "unknown",
]
DistroFamily = Literal["rhel-like", "debian-like", "windows", "unknown"]
Confidence = Literal["high", "medium", "low"]


# Windows build → product mapping. Build numbers are stable across CUs; the
# major.minor pair from `Win32_OperatingSystem.Version` (e.g. "10.0.20348")
# tells us which release we're on. New servers are added here as Microsoft
# ships them.
_WINDOWS_BUILD_TO_DISTRO: dict[int, DistroName] = {
    17763: "windows-server-2019",
    20348: "windows-server-2022",
    26100: "windows-server-2025",
}


# Map os-release ID → canonical distro name. Anything not listed here lands
# in "unknown" and we degrade gracefully via the modern-Linux defaults.
_DISTRO_BY_ID: dict[str, DistroName] = {
    "rhel": "rhel",
    "fedora": "fedora",
    "rocky": "rocky",
    "almalinux": "alma",
    "centos": "centos",
    "ubuntu": "ubuntu",
    "debian": "debian",
}


def _family_of(distro: DistroName, id_like: str) -> DistroFamily:
    """RHEL-like vs Debian-like vs Windows, falling back to unknown.

    Picks the family from the canonical distro first, then from
    ``ID_LIKE`` if the distro itself wasn't recognized — which is how
    obscure RHEL forks end up in the right bucket.
    """
    if distro in {"rhel", "fedora", "rocky", "alma", "centos"}:
        return "rhel-like"
    if distro in {"ubuntu", "debian"}:
        return "debian-like"
    if distro.startswith("windows"):
        return "windows"
    tokens = id_like.lower().split()
    if any(t in {"rhel", "fedora", "centos"} for t in tokens):
        return "rhel-like"
    if any(t in {"debian", "ubuntu"} for t in tokens):
        return "debian-like"
    return "unknown"


def _split_version(raw: str) -> tuple[int, int]:
    """Parse VERSION_ID like '9.2', '22.04', '7' → (major, minor)."""
    if not raw:
        return (0, 0)
    parts = raw.split(".")
    try:
        major = int(parts[0])
    except (ValueError, IndexError):
        return (0, 0)
    minor = 0
    if len(parts) > 1:
        try:
            minor = int(parts[1])
        except ValueError:
            minor = 0
    return major, minor


def _parse_os_release(raw: str) -> dict[str, str]:
    """Lightweight parser — strips the surrounding ``"`` shell quoting."""
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        elif value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        out[key.strip()] = value
    return out


@dataclass(frozen=True)
class OSProfile:
    """Structured snapshot of the target VM's OS — produced once per collection.

    Drives:
      - ``CommandSet`` selection in ``app.core.commands``
      - The LLM's understanding of which OS conventions apply
      - The VM detail panel's "Detected OS" badge
    """

    distro: DistroName
    distro_family: DistroFamily
    major_version: int
    minor_version: int
    kernel_version: str
    architecture: str
    is_systemd: bool
    pretty_name: str
    detection_confidence: Confidence

    @property
    def version_label(self) -> str:
        if self.major_version == 0:
            return ""
        if self.minor_version:
            return f"{self.major_version}.{self.minor_version}"
        return str(self.major_version)

    def to_dict(self) -> dict:
        return asdict(self)


def detect(
    *,
    os_release_text: str,
    uname_r: str = "",
    uname_m: str = "",
) -> OSProfile:
    """Build an ``OSProfile`` from the three commands the collector runs.

    Inputs are the raw stdout strings — parsing is centralized here so the
    collector stays I/O-shaped and the parser stays unit-testable without
    spinning up SSH.
    """
    fields = _parse_os_release(os_release_text or "")
    distro_id = fields.get("ID", "").strip().lower()
    distro: DistroName = _DISTRO_BY_ID.get(distro_id, "unknown")
    id_like = fields.get("ID_LIKE", "")
    family = _family_of(distro, id_like)
    major, minor = _split_version(fields.get("VERSION_ID", ""))
    pretty = fields.get("PRETTY_NAME", "") or fields.get("NAME", "")

    if distro == "unknown":
        confidence: Confidence = "low"
    elif major == 0:
        # Distro recognized but couldn't parse a version — still useful but
        # downstream branching can't lean on the version number.
        confidence = "medium"
    else:
        confidence = "high"

    return OSProfile(
        distro=distro,
        distro_family=family,
        major_version=major,
        minor_version=minor,
        kernel_version=(uname_r or "").strip(),
        architecture=(uname_m or "").strip(),
        # Every OS we currently target uses systemd as PID 1. If we ever
        # grow alpine/openrc/init.d support, flip this off based on a
        # ``systemctl --version`` exit-code probe.
        is_systemd=True,
        pretty_name=pretty,
        detection_confidence=confidence,
    )


def detect_windows(*, get_ciminstance_json: str) -> OSProfile:
    """Build an ``OSProfile`` from ``Get-CimInstance Win32_OperatingSystem``.

    Expected input is the stdout of:

        powershell -Command "Get-CimInstance Win32_OperatingSystem |
          ConvertTo-Json -Compress"

    On Server 2019/2022 the relevant fields are ``Caption``, ``Version``
    (e.g. "10.0.20348"), ``BuildNumber``, and ``OSArchitecture``. We're
    deliberately tolerant of missing fields — Windows occasionally
    surfaces partial CIM output, especially under hardened SCCM
    configurations.
    """
    try:
        body = json.loads(get_ciminstance_json or "")
    except (json.JSONDecodeError, TypeError):
        return _windows_unknown_profile("Win32_OperatingSystem returned non-JSON")

    if isinstance(body, list) and body:
        body = body[0]
    if not isinstance(body, dict):
        return _windows_unknown_profile("Win32_OperatingSystem JSON not an object")

    version = str(body.get("Version") or "").strip()
    build_raw = body.get("BuildNumber") or body.get("Build")
    try:
        build = int(build_raw) if build_raw is not None else 0
    except (ValueError, TypeError):
        build = 0
    if build == 0 and version:
        # Fall back to parsing build out of "10.0.20348".
        parts = version.split(".")
        if len(parts) >= 3:
            try:
                build = int(parts[2])
            except ValueError:
                build = 0

    distro: DistroName = _WINDOWS_BUILD_TO_DISTRO.get(build, "windows-unknown")
    caption = str(body.get("Caption") or "").strip()
    arch = str(body.get("OSArchitecture") or "").strip()

    # Windows uses Major.Minor.Build.Revision. We surface major separately
    # (10 for Server 2019/2022, 11 for Server 2025) and store build in
    # minor_version so the existing UI string "<distro> <major>.<minor>"
    # produces "windows-server-2022 10.20348" — readable and unambiguous.
    major = 10
    if version:
        try:
            major = int(version.split(".")[0])
        except (ValueError, IndexError):
            major = 10

    if distro == "windows-unknown":
        confidence: Confidence = "low" if not caption else "medium"
    else:
        confidence = "high" if caption else "medium"

    return OSProfile(
        distro=distro,
        distro_family="windows",
        major_version=major,
        minor_version=build,
        kernel_version=version,  # full Major.Minor.Build.Revision
        architecture=arch,
        # Windows has no systemd. Downstream code uses this to decide
        # whether to issue systemctl-style commands; for Windows the
        # CommandSet routes through PowerShell instead.
        is_systemd=False,
        pretty_name=caption,
        detection_confidence=confidence,
    )


def _windows_unknown_profile(reason: str) -> OSProfile:
    """Windows-shaped fallback when the CIM probe ran but we couldn't make
    sense of it — keeps ``distro_family="windows"`` so the CommandSet
    factory still routes to the PowerShell branch."""
    return OSProfile(
        distro="windows-unknown",
        distro_family="windows",
        major_version=0,
        minor_version=0,
        kernel_version="",
        architecture="",
        is_systemd=False,
        pretty_name=reason,
        detection_confidence="low",
    )


# Single instance returned when a caller can't supply os-release at all
# (e.g., the SSH session refused before we read it). Used by the collector
# as a last-resort fallback so callers still get an OSProfile.
UNKNOWN_PROFILE = OSProfile(
    distro="unknown",
    distro_family="unknown",
    major_version=0,
    minor_version=0,
    kernel_version="",
    architecture="",
    is_systemd=True,
    pretty_name="",
    detection_confidence="low",
)
