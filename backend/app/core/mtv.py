"""
MTV / Forklift migration plan YAML generator.

Given a wave (subset of VMs from a MigrationPlan) plus the per-VM source +
target mapping the operator captured during enrollment, this module emits a
multi-document YAML containing:

  - one NetworkMap (forklift.konveyor.io/v1beta1) for every source portgroup
    referenced by VMs in the wave
  - one StorageMap (forklift.konveyor.io/v1beta1) for every source datastore
    referenced by VMs in the wave
  - one Plan (forklift.konveyor.io/v1beta1) referencing both maps and listing
    the wave's VMs (warm migration by default)

The output is the exact text the operator pipes into ``oc apply -f``.

The Provider resources (source vSphere + destination OCP-Virt host) are
*not* generated here — they're cluster-scoped MTV configuration created
once by the platform team. We only reference them by name/namespace.

When a :class:`ResourceMapping` is supplied via :class:`MappingResolver`
the source-network and source-datastore lookups go through the mapping
first. Per-VM ``target_*`` fields remain a legacy fallback so existing
plans that predate target-cluster registration still produce valid YAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from app.core.config import settings


class MTVGenerationError(ValueError):
    """Raised when the wave is missing data required to produce a valid plan."""


@dataclass(frozen=True)
class MappingResolver:
    """Resolves source vSphere resources to target OCP names via a
    :class:`ResourceMapping` payload.

    A ``None`` resolver means "no mapping" — callers fall back to
    per-VM ``target_*`` fields. The resolver is intentionally a plain
    dataclass so the MTV generator stays free of database-layer
    coupling and is easy to fake in tests.
    """

    network_mappings: list[dict] = field(default_factory=list)
    storage_mappings: list[dict] = field(default_factory=list)
    namespace_mappings: list[dict] = field(default_factory=list)
    default_target_namespace: str = ""

    def resolve_network(self, source_network: str) -> dict | None:
        for entry in self.network_mappings:
            if entry.get("source_network") == source_network:
                if not entry.get("target_network_name"):
                    return None
                return {
                    "name": entry["target_network_name"],
                    "namespace": entry.get("target_namespace") or "",
                    "type": entry.get("target_network_type") or "nad",
                }
        return None

    def resolve_storage(self, source_datastore: str) -> str | None:
        for entry in self.storage_mappings:
            if entry.get("source_datastore") == source_datastore:
                return entry.get("target_storage_class") or None
        return None

    def resolve_namespace(self, vm: dict) -> str | None:
        """Walk namespace_mappings in order; first matching criteria wins."""
        for entry in self.namespace_mappings:
            crit = entry.get("criteria") or "default"
            value = entry.get("criteria_value") or ""
            target_ns = entry.get("target_namespace") or ""
            if not target_ns:
                continue
            if crit == "default":
                return target_ns
            if crit == "environment" and (vm.get("environment") or "") == value:
                return target_ns
            if crit == "application" and (vm.get("application_hint") or "") == value:
                return target_ns
            if crit == "vcenter_folder" and (vm.get("vcenter_folder") or "") == value:
                return target_ns
        return None


@dataclass(frozen=True)
class WaveContext:
    """Inputs the YAML generator needs in addition to the per-VM rows."""

    plan_id: int
    wave_number: int
    rationale: str
    namespace: str
    source_provider: str
    destination_provider: str
    default_target_namespace: str

    @classmethod
    def from_settings(
        cls,
        plan_id: int,
        wave_number: int,
        rationale: str,
    ) -> WaveContext:
        return cls(
            plan_id=plan_id,
            wave_number=wave_number,
            rationale=rationale,
            namespace=settings.mtv_namespace,
            source_provider=settings.mtv_source_provider,
            destination_provider=settings.mtv_destination_provider,
            default_target_namespace=settings.mtv_default_target_namespace,
        )


def _provider_pair(ctx: WaveContext) -> dict[str, Any]:
    return {
        "source": {"name": ctx.source_provider, "namespace": ctx.namespace},
        "destination": {"name": ctx.destination_provider, "namespace": ctx.namespace},
    }


def _resource_basename(ctx: WaveContext) -> str:
    return f"vv-plan-{ctx.plan_id}-wave-{ctx.wave_number}"


def _build_network_map(
    ctx: WaveContext,
    vms: list[dict],
    resolver: MappingResolver | None,
) -> dict[str, Any]:
    """One mapping entry per unique source portgroup.

    First VM that references a given source network wins for the destination
    side — operators who need divergent mappings should split the wave or
    edit the YAML before applying.
    """
    seen: dict[str, dict[str, Any]] = {}
    for vm in vms:
        nad_fallback = vm.get("target_network_attachment") or ""
        ns_fallback = vm.get("target_namespace") or ctx.default_target_namespace
        for src in vm.get("vsphere_networks") or []:
            if not src or src in seen:
                continue
            mapped = resolver.resolve_network(src) if resolver else None
            if mapped is not None:
                if mapped["type"] == "pod":
                    destination = {"type": "pod"}
                else:
                    destination = {
                        "type": "multus",
                        "name": mapped["name"],
                        "namespace": mapped["namespace"] or ns_fallback,
                    }
            elif nad_fallback:
                destination = {
                    "type": "multus",
                    "name": nad_fallback,
                    "namespace": ns_fallback,
                }
            else:
                # No mapping entry and no per-VM NAD — fall back to the
                # pod network so the YAML stays valid; operators can edit
                # before applying.
                destination = {"type": "pod"}
            seen[src] = {
                "source": {"name": src},
                "destination": destination,
            }

    return {
        "apiVersion": "forklift.konveyor.io/v1beta1",
        "kind": "NetworkMap",
        "metadata": {
            "name": f"{_resource_basename(ctx)}-netmap",
            "namespace": ctx.namespace,
        },
        "spec": {
            "map": list(seen.values()),
            "provider": _provider_pair(ctx),
        },
    }


def _build_storage_map(
    ctx: WaveContext,
    vms: list[dict],
    resolver: MappingResolver | None,
) -> dict[str, Any]:
    """One mapping entry per unique source datastore (first-VM-wins)."""
    seen: dict[str, dict[str, Any]] = {}
    for vm in vms:
        sc_fallback = vm.get("target_storage_class") or ""
        for src in vm.get("vsphere_datastores") or []:
            if not src or src in seen:
                continue
            sc = (resolver.resolve_storage(src) if resolver else None) or sc_fallback
            if not sc:
                # Skip rather than emit an obviously-invalid mapping. The
                # plan-level error below catches the "no entries at all"
                # case.
                continue
            seen[src] = {
                "source": {"name": src},
                "destination": {"storageClass": sc},
            }

    return {
        "apiVersion": "forklift.konveyor.io/v1beta1",
        "kind": "StorageMap",
        "metadata": {
            "name": f"{_resource_basename(ctx)}-storagemap",
            "namespace": ctx.namespace,
        },
        "spec": {
            "map": list(seen.values()),
            "provider": _provider_pair(ctx),
        },
    }


def _build_plan(
    ctx: WaveContext,
    vms: list[dict],
    netmap_name: str,
    storagemap_name: str,
    resolver: MappingResolver | None,
) -> dict[str, Any]:
    def _ns_for(vm: dict) -> str:
        if resolver is not None:
            mapped = resolver.resolve_namespace(vm)
            if mapped:
                return mapped
        return vm.get("target_namespace") or ctx.default_target_namespace

    resolved_namespaces = [_ns_for(vm) for vm in vms]
    plan_target_ns = resolved_namespaces[0] if resolved_namespaces else ctx.default_target_namespace

    plan_vms: list[dict[str, Any]] = []
    for vm, vm_ns in zip(vms, resolved_namespaces, strict=True):
        entry: dict[str, Any] = {"name": vm["name"]}
        # Per-VM target namespace override only when it diverges from the
        # plan-level target — keeps the YAML compact in the common case.
        if vm_ns and vm_ns != plan_target_ns:
            entry["namespace"] = vm_ns
        plan_vms.append(entry)

    return {
        "apiVersion": "forklift.konveyor.io/v1beta1",
        "kind": "Plan",
        "metadata": {
            "name": _resource_basename(ctx),
            "namespace": ctx.namespace,
        },
        "spec": {
            "description": ctx.rationale or f"VirtValidate wave {ctx.wave_number}",
            "warm": True,
            "targetNamespace": plan_target_ns,
            "provider": _provider_pair(ctx),
            "map": {
                "network": {"name": netmap_name, "namespace": ctx.namespace},
                "storage": {"name": storagemap_name, "namespace": ctx.namespace},
            },
            "vms": plan_vms,
        },
    }


def generate_wave_yaml(
    ctx: WaveContext,
    vms: list[dict],
    resolver: MappingResolver | None = None,
) -> str:
    """Render the wave's NetworkMap + StorageMap + Plan as a multi-doc YAML.

    ``vms`` items must contain at least: ``name`` (str), and optionally
    ``vsphere_networks`` / ``vsphere_datastores`` (list[str]),
    ``target_namespace`` / ``target_storage_class`` /
    ``target_network_attachment`` (str).

    ``resolver`` is the optional :class:`ResourceMapping`-backed lookup
    that overrides per-VM target_* fields. When ``None`` the function
    falls back to the legacy per-VM behavior so plans generated before
    target-cluster registration keep working.
    """
    if not vms:
        raise MTVGenerationError("Cannot generate a plan for an empty wave")

    netmap = _build_network_map(ctx, vms, resolver)
    storagemap = _build_storage_map(ctx, vms, resolver)
    plan = _build_plan(
        ctx,
        vms,
        netmap_name=netmap["metadata"]["name"],
        storagemap_name=storagemap["metadata"]["name"],
        resolver=resolver,
    )

    if not netmap["spec"]["map"]:
        raise MTVGenerationError(
            f"Wave {ctx.wave_number} has no source networks across its VMs — "
            "set vsphere_networks on at least one VM before generating MTV YAML"
        )
    if not storagemap["spec"]["map"]:
        raise MTVGenerationError(
            f"Wave {ctx.wave_number} has no source-to-storageclass mappings — "
            "set vsphere_datastores and target_storage_class on at least one VM"
        )

    return yaml.safe_dump_all(
        [netmap, storagemap, plan],
        sort_keys=False,
        default_flow_style=False,
    )
