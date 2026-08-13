"""Read-only command gate for the SSH collector.

VirtValidate SSHes into **production** servers. Every command the collector
runs is sourced from the fixed per-OS dispatch table in ``app.core.commands``
and is read-only by construction — the agent inspects state, it never
mutates a host. This module is the *enforcement* of that property: a single
choke point (``SSHCollector._run``) calls :func:`assert_read_only` before
``exec_command``, so a future change that introduces a mutating command
cannot reach a production host silently. It fails closed.

Design — two independent layers, both must pass:

  1. **Deny layer** — reject any command matching a known mutation pattern
     (``rm``, ``systemctl start``, ``Set-*``, file-write redirection, …).
     Catches a mutating *subcommand* of an otherwise-allowed tool
     (``crontab -r``, ``iptables -A``).
  2. **Allow layer** — every pipeline segment's leading program must be a
     known read-only tool. Catches a *novel* mutating tool the deny layer
     hasn't enumerated.

Routing is by command **content**, not the caller's declared OS family:
OS detection issues a PowerShell probe before the family is known, so a
``powershell``/``pwsh`` prefix routes to the PowerShell rules regardless of
``os_family``. ``os_family`` is retained for clearer error messages.

This gate is intentionally conservative. When ``commands.py`` grows a new
read-only probe, add its leading tool to the allowlist here in the same
change — that friction is the point: every command run against prod is an
explicit, auditable decision.
"""

from __future__ import annotations

import re
import shlex

# ---------------------------------------------------------------------------
# POSIX
# ---------------------------------------------------------------------------

# Leading programs the collector is allowed to invoke on a POSIX host. Each
# corresponds to a read-only probe in ``commands.py`` (or a read-only filter
# in a pipeline, e.g. ``tail``/``cut``). Dual-use tools (systemctl, crontab,
# nft, iptables, find) are gated further by the deny layer below.
_POSIX_ALLOWED_PROGRAMS: frozenset[str] = frozenset(
    {
        "ip",
        "ss",
        "cat",
        "findmnt",
        "find",
        "systemctl",
        "crontab",
        "uname",
        "hostname",
        "nft",
        "iptables",
        "ip6tables",
        "chronyc",
        "ntpq",
        "timedatectl",
        "ufw",
        "dnf",
        "yum",
        "dpkg-query",
        "cut",
        "tail",
        "head",
        "true",
        ":",
    }
)

# Global deny — patterns dangerous in ANY position, regardless of which tool
# leads the segment. Kept narrow so they can't false-match a file *path*
# (e.g. ``/etc/passwd``, ``/etc/crontab``); tool-name mutation is handled per
# segment by the allowlist + dual-use rules below, not by scanning the whole
# string for command names.
_POSIX_GLOBAL_DENY: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"\$\(",  # command substitution: $(...)
        r"`",  # command substitution: backticks
        # File-write redirection. ``2>/dev/null``, ``2>&1`` and ``>/dev/null``
        # are harmless and explicitly permitted; anything else writing a file
        # is rejected.
        r"(?<![\d&])>>?\s*(?!/dev/null\b)(?!&)\S",
    )
)

# Dual-use tools (on the allowlist) → a regex that, if it matches the SEGMENT
# that tool leads, marks the invocation as mutating. ``crontab`` is handled
# specially (allowed only with ``-l``). A tool absent here is read-only for
# every invocation once its program is allow-listed.
_DUALUSE_MUTATION: dict[str, re.Pattern[str]] = {
    "systemctl": re.compile(
        r"\b(start|stop|restart|reload|try-restart|enable|disable|mask|unmask|"
        r"kill|isolate|set-property|edit|set-default|reset-failed|daemon-reload)\b"
    ),
    "nft": re.compile(r"\bnft\b\s+(add|delete|flush|insert|create|replace|rename)\b"),
    "iptables": re.compile(
        r"\s(-A|-I|-D|-R|-F|-X|-N|-P|--append|--insert|--delete|--replace|"
        r"--flush|--policy|--new-chain|--delete-chain)\b"
    ),
    "find": re.compile(r"\s-(delete|exec|execdir|ok|okdir|fprintf?|fls)\b"),
    "dnf": re.compile(
        r"\bdnf\b\s+(install|remove|erase|upgrade|update|reinstall|autoremove|"
        r"downgrade|distro-sync|localinstall|groupinstall|groupremove)\b"
    ),
    "yum": re.compile(
        r"\byum\b\s+(install|remove|erase|upgrade|update|reinstall|autoremove|"
        r"downgrade|localinstall|groupinstall|groupremove)\b"
    ),
    "ufw": re.compile(
        r"\bufw\b\s+(enable|disable|allow|deny|reject|limit|delete|reset|reload|default)\b"
    ),
}
# iptables and ip6tables share the same flag grammar.
_DUALUSE_MUTATION["ip6tables"] = _DUALUSE_MUTATION["iptables"]

# ---------------------------------------------------------------------------
# PowerShell (Windows over OpenSSH)
# ---------------------------------------------------------------------------

# Approved PowerShell cmdlet verbs — all read-only ("get the state, shape it,
# serialize it"). Any cmdlet whose verb is outside this set is rejected.
_PS_ALLOWED_VERBS: frozenset[str] = frozenset(
    {
        "Get",
        "Select",
        "Where",
        "Sort",
        "Measure",
        "Compare",
        "Group",
        "ConvertTo",
        "ConvertFrom",
        "Format",
        "Out",  # only Out-String (no Out-File) — see deny layer
    }
)

# Verbs that mutate. Explicit deny so a typo'd allowlist can't let them slip.
_PS_DENY_PATTERN = re.compile(
    r"\b(Set|New|Remove|Stop|Start|Restart|Suspend|Resume|Add|Clear|Disable|"
    r"Enable|Rename|Move|Copy|Install|Uninstall|Register|Unregister|Invoke|"
    r"Reset|Update|Write|Push|Pop|Out-File|Export|Import)-",
    re.IGNORECASE,
)

# Any cmdlet-shaped token: Verb-Noun.
_PS_CMDLET_TOKEN = re.compile(r"\b([A-Z][a-zA-Z]+)-[A-Z][a-zA-Z0-9]+\b")

_PS_PREFIXES = ("powershell", "pwsh")


class SSHCommandNotAllowed(RuntimeError):
    """A command failed the read-only gate and was refused before execution.

    Carries the offending command so the audit layer can record exactly what
    was blocked. The SSH layer maps this to ``failure_category="command_failed"``.
    """

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(f"Refused non-read-only SSH command ({reason}): {command!r}")
        self.command = command
        self.reason = reason


def _split_segments(command: str) -> list[str]:
    """Split a shell command into pipeline/sequence segments.

    Splits on ``|``, ``||``, ``&&``, ``;`` and ``&`` so each segment's leading
    program can be checked independently. Crude but sufficient — the deny layer
    catches the dangerous constructs (substitution, redirection) that this
    naive split would otherwise mishandle.
    """
    parts = re.split(r"\|\||&&|[|;&]", command)
    return [p.strip() for p in parts if p.strip()]


def _leading_program(segment: str) -> str:
    """First token of a segment, best-effort. Empty string if unparseable."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        # Unbalanced quotes etc. — let the caller reject.
        return ""
    return tokens[0] if tokens else ""


def _assert_powershell_read_only(command: str) -> None:
    if _PS_DENY_PATTERN.search(command):
        raise SSHCommandNotAllowed(command, "mutating PowerShell cmdlet")
    for match in _PS_CMDLET_TOKEN.finditer(command):
        verb = match.group(1)
        if verb not in _PS_ALLOWED_VERBS:
            raise SSHCommandNotAllowed(command, f"PowerShell verb {verb!r} not allow-listed")
    # Out-* is only allowed as Out-String; Out-File already blocked above, but
    # reject any other Out-* defensively.
    for match in re.finditer(r"\bOut-([A-Z][a-zA-Z]+)\b", command):
        if match.group(1) != "String":
            raise SSHCommandNotAllowed(command, f"Out-{match.group(1)} not allow-listed")


def _assert_posix_read_only(command: str) -> None:
    for pat in _POSIX_GLOBAL_DENY:
        if pat.search(command):
            raise SSHCommandNotAllowed(command, "command substitution or file redirection")
    segments = _split_segments(command)
    if not segments:
        raise SSHCommandNotAllowed(command, "empty command")
    for seg in segments:
        prog = _leading_program(seg)
        if not prog:
            raise SSHCommandNotAllowed(command, "unparseable segment")
        # Normalize an absolute path (/usr/bin/ip) to its basename.
        base = prog.rsplit("/", 1)[-1]
        if base not in _POSIX_ALLOWED_PROGRAMS:
            raise SSHCommandNotAllowed(command, f"program {base!r} not allow-listed")
        # crontab is read-only ONLY as a listing (``-l``); ``-r`` removes and a
        # bare file argument installs.
        if base == "crontab":
            if not re.search(r"\s-l\b", seg):
                raise SSHCommandNotAllowed(command, "crontab without -l (read-only listing)")
            continue
        deny = _DUALUSE_MUTATION.get(base)
        if deny is not None and deny.search(seg):
            raise SSHCommandNotAllowed(command, f"mutating {base} subcommand")


def assert_read_only(command: str, os_family: str = "unknown") -> None:
    """Raise :class:`SSHCommandNotAllowed` unless ``command`` is read-only.

    Routing is by content: a ``powershell``/``pwsh`` prefix uses the
    PowerShell rules (so the OS-detection probe is validated correctly even
    before the family is known); ``w32tm /query`` is the one non-PowerShell
    Windows probe; everything else uses the POSIX rules. ``os_family`` is
    advisory, used only for error context.
    """
    stripped = command.strip()
    if not stripped:
        raise SSHCommandNotAllowed(command, "empty command")

    first = stripped.split(None, 1)[0].rsplit("/", 1)[-1].lower()
    if first in _PS_PREFIXES:
        _assert_powershell_read_only(stripped)
        return
    if first == "w32tm":
        # Read-only only when querying; w32tm can also reconfigure time sync.
        if not re.search(r"\bw32tm\b\s+/query\b", stripped):
            raise SSHCommandNotAllowed(command, "w32tm without /query")
        return
    _assert_posix_read_only(stripped)
