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
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from app.core.config import settings


class MTVGenerationError(ValueError):
    """Raised when the wave is missing data required to produce a valid plan."""


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


def _build_network_map(ctx: WaveContext, vms: list[dict]) -> dict[str, Any]:
    """One mapping entry per unique source portgroup.

    First VM that references a given source network wins for the destination
    side — operators who need divergent mappings should split the wave or
    edit the YAML before applying.
    """
    seen: dict[str, dict[str, Any]] = {}
    for vm in vms:
        nad = vm.get("target_network_attachment") or ""
        target_ns = vm.get("target_namespace") or ctx.default_target_namespace
        for src in vm.get("vsphere_networks") or []:
            if not src or src in seen:
                continue
            if nad:
                destination = {
                    "type": "multus",
                    "name": nad,
                    "namespace": target_ns,
                }
            else:
                # No NAD specified — fall back to the pod network so the
                # YAML is still valid; operators can edit before applying.
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


def _build_storage_map(ctx: WaveContext, vms: list[dict]) -> dict[str, Any]:
    """One mapping entry per unique source datastore (first-VM-wins)."""
    seen: dict[str, dict[str, Any]] = {}
    for vm in vms:
        sc = vm.get("target_storage_class") or ""
        for src in vm.get("vsphere_datastores") or []:
            if not src or src in seen:
                continue
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
) -> dict[str, Any]:
    target_namespaces = [
        vm.get("target_namespace") for vm in vms if vm.get("target_namespace")
    ]
    plan_target_ns = target_namespaces[0] if target_namespaces else ctx.default_target_namespace

    plan_vms: list[dict[str, Any]] = []
    for vm in vms:
        entry: dict[str, Any] = {"name": vm["name"]}
        # Per-VM target namespace override only when it diverges from the
        # plan-level target — keeps the YAML compact in the common case.
        vm_ns = vm.get("target_namespace")
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


def generate_wave_yaml(ctx: WaveContext, vms: list[dict]) -> str:
    """Render the wave's NetworkMap + StorageMap + Plan as a multi-doc YAML.

    ``vms`` items must contain at least: ``name`` (str), and optionally
    ``vsphere_networks`` / ``vsphere_datastores`` (list[str]),
    ``target_namespace`` / ``target_storage_class`` /
    ``target_network_attachment`` (str).
    """
    if not vms:
        raise MTVGenerationError("Cannot generate a plan for an empty wave")

    netmap = _build_network_map(ctx, vms)
    storagemap = _build_storage_map(ctx, vms)
    plan = _build_plan(
        ctx,
        vms,
        netmap_name=netmap["metadata"]["name"],
        storagemap_name=storagemap["metadata"]["name"],
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
