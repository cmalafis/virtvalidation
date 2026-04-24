"""
Baseline profile synthesis.

Aggregates multiple BaselineSnapshot rows for a VM into a single profile that
summarizes what is stable vs transient across collections.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable

from app.models.vm import BaselineSnapshot
from app.schemas.vm import BaselineProfile


def _port_key(entry: dict) -> tuple:
    return (entry.get("proto"), entry.get("address"), entry.get("port"))


def _mount_key(entry: dict) -> tuple:
    return (entry.get("target"), entry.get("source"), entry.get("fstype"))


def synthesize_profile(vm_id: int, snapshots: Iterable[BaselineSnapshot]) -> BaselineProfile:
    snapshots = list(snapshots)

    if not snapshots:
        return BaselineProfile(
            vm_id=vm_id,
            snapshot_count=0,
            first_collected_at=None,
            last_collected_at=None,
            latest_meta={},
            services=[],
            open_ports=[],
            stable_mounts=[],
            dns_servers=[],
            interfaces={},
        )

    latest = snapshots[-1].raw_data or {}
    total = len(snapshots)

    service_counts: Counter[str] = Counter()
    port_counts: Counter[tuple] = Counter()
    port_examples: dict[tuple, dict] = {}
    mount_counts: Counter[tuple] = Counter()
    mount_examples: dict[tuple, dict] = {}
    dns_seen: set[str] = set()
    interfaces: dict[str, dict] = {}

    for snap in snapshots:
        data = snap.raw_data or {}
        for svc in data.get("services", []) or []:
            unit = svc.get("unit")
            if unit:
                service_counts[unit] += 1
        for port in data.get("ports", []) or []:
            key = _port_key(port)
            port_counts[key] += 1
            port_examples.setdefault(key, port)
        for mount in data.get("mounts", []) or []:
            key = _mount_key(mount)
            mount_counts[key] += 1
            mount_examples.setdefault(key, mount)
        network = data.get("network", {}) or {}
        for ns in network.get("dns", []) or []:
            dns_seen.add(ns)
        for name, iface in (network.get("interfaces", {}) or {}).items():
            interfaces[name] = iface

    # "Stable" = present in every snapshot we have.
    services = sorted(u for u, c in service_counts.items() if c == total)
    open_ports = [port_examples[k] for k, c in port_counts.items() if c == total]
    stable_mounts = [mount_examples[k] for k, c in mount_counts.items() if c == total]

    return BaselineProfile(
        vm_id=vm_id,
        snapshot_count=total,
        first_collected_at=snapshots[0].collected_at,
        last_collected_at=snapshots[-1].collected_at,
        latest_meta=latest.get("meta", {}) or {},
        services=services,
        open_ports=open_ports,
        stable_mounts=stable_mounts,
        dns_servers=sorted(dns_seen),
        interfaces=interfaces,
    )
