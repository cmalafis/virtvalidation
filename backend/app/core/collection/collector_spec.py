"""Pass-1 deterministic probe catalog.

This module describes what the collection engine captures from each VM in
v1 (deterministic, no LLM). It exists primarily as **metadata** — the
actual collection logic lives in :mod:`app.core.ssh` via ``SSHCollector``,
which already runs the relevant commands and parses their output. The
catalog here documents the contract: what probes are guaranteed to be in
``CollectionResult.collected_data`` after a successful collection, and
under what catalog version.

Why a version constant: a later session will add LLM-directed probing
that selects workload-specific probes from a larger catalog. Recording
the catalog version + the actual probes that ran on each baseline lets
validation re-run exactly the same probe set, and lets operators see
"this baseline was captured under pass1-v1; this one under pass2-llm-v1"
in the audit log. Without that, the diff-vs-baseline contract gets fuzzy.

Pass-1 coverage (mapped to ``SSHCollector`` subsystems where possible):

  - ``meta``       — host, hostname, kernel, OS profile (existing)
  - ``services``   — systemd unit state incl. failed services (existing)
  - ``network``    — interfaces, routes, DNS (existing)
  - ``ports``      — listening ports + owning process (existing)
  - ``mounts``     — filesystem mount table + usage (existing)
  - ``cron``       — system + per-user cron (existing)

Items NOT yet collected by ``SSHCollector`` that the brief lists are
deferred to a follow-up patch alongside actual parsers; they're declared
here as future probes (``available=False``) so the spec is the
canonical reference for what will eventually live in this catalog.
Until then, the orchestrator only counts the ``available=True`` probes
when populating :attr:`Baseline.probes_run`.
"""

from __future__ import annotations

from dataclasses import dataclass


PASS1_CATALOG_VERSION = "pass1-v1"


@dataclass(frozen=True)
class ProbeSpec:
    """One probe in the Pass-1 catalog.

    ``available=True`` means the existing collector populates a field
    matching ``data_key`` in the collected_data dict. ``available=False``
    means the probe is part of the v1 contract but not yet wired to a
    parser — the spec entry exists to keep the catalog self-documenting.
    """

    name: str
    data_key: str
    description: str
    available: bool


_PASS1_SPEC: tuple[ProbeSpec, ...] = (
    ProbeSpec(
        name="vm_identity",
        data_key="meta",
        description="host, hostname, kernel, OS profile, collected_at",
        available=True,
    ),
    ProbeSpec(
        name="systemd_services",
        data_key="services",
        description=(
            "Enabled / running / failed systemd services with unit + sub state"
        ),
        available=True,
    ),
    ProbeSpec(
        name="network_interfaces",
        data_key="network",
        description="Interface IPs, default route, DNS resolvers",
        available=True,
    ),
    ProbeSpec(
        name="listening_ports",
        data_key="ports",
        description="Listening TCP/UDP ports with state + address",
        available=True,
    ),
    ProbeSpec(
        name="mounts",
        data_key="mounts",
        description="Filesystem mounts with source, target, fstype, options",
        available=True,
    ),
    ProbeSpec(
        name="cron",
        data_key="cron",
        description="System + per-user cron jobs",
        available=True,
    ),
    # ---- Probes declared by the brief but not yet wired to parsers.
    # They live in the catalog so when a follow-up patch adds them, the
    # contract is already documented and ``Baseline.probes_run`` becomes
    # the source of truth for what each captured baseline actually saw.
    ProbeSpec(
        name="uptime_load",
        data_key="uptime",
        description="uptime + 1/5/15 load averages",
        available=False,
    ),
    ProbeSpec(
        name="memory",
        data_key="memory",
        description="total / used / available / swap",
        available=False,
    ),
    ProbeSpec(
        name="disk_usage",
        data_key="disk",
        description="Per-mount capacity / used / available / inode usage",
        available=False,
    ),
    ProbeSpec(
        name="time_sync",
        data_key="time_sync",
        description="chrony / ntpd state (in-sync, drift)",
        available=False,
    ),
    ProbeSpec(
        name="pending_reboot",
        data_key="pending_reboot",
        description="Kernel updated without reboot indicator",
        available=False,
    ),
    ProbeSpec(
        name="top_processes",
        data_key="top_processes",
        description="Top by memory + CPU, total process count",
        available=False,
    ),
    ProbeSpec(
        name="restart_loops",
        data_key="restart_loops",
        description="Services in a tight restart cycle",
        available=False,
    ),
    ProbeSpec(
        name="established_outbound",
        data_key="established_outbound",
        description="Established outbound TCP connections",
        available=False,
    ),
    ProbeSpec(
        name="systemd_timers",
        data_key="systemd_timers",
        description="Active systemd timers (last + next trigger)",
        available=False,
    ),
    ProbeSpec(
        name="journal_errors",
        data_key="journal_errors",
        description="Journal/syslog error counts over last 24h",
        available=False,
    ),
    ProbeSpec(
        name="log_mtimes",
        data_key="log_mtimes",
        description="mtimes of key /var/log files (are they still written?)",
        available=False,
    ),
    ProbeSpec(
        name="package_inventory",
        data_key="package_inventory",
        description="Installed package count or hash digest",
        available=False,
    ),
    ProbeSpec(
        name="tls_cert_expiry",
        data_key="tls_cert_expiry",
        description="Expiry dates for TLS certs on listening ports",
        available=False,
    ),
)


def pass1_spec() -> tuple[ProbeSpec, ...]:
    """Return the full Pass-1 probe catalog (available + future)."""
    return _PASS1_SPEC


def pass1_probe_names(*, available_only: bool = True) -> list[str]:
    """Return the list of probe names recorded in :attr:`Baseline.probes_run`.

    By default returns only the probes that the engine actually produces
    in v1 (``available=True``). The orchestrator uses this list to populate
    :attr:`Baseline.probes_run` so the validation step knows exactly which
    probes to compare against.
    """
    return [
        p.name for p in _PASS1_SPEC if (not available_only or p.available)
    ]


def pass1_data_keys() -> set[str]:
    """Return the set of top-level keys the available probes contribute."""
    return {p.data_key for p in _PASS1_SPEC if p.available}
