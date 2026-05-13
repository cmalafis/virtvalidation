"""Stage 0 — mapping coverage validation.

Run before any structural work on a plan. Every selected VM must have:

  - a target namespace (either ``vm.target_namespace`` is set, or the
    mapping's namespace strategy resolves the VM, or a default exists),
  - a target NetworkAttachmentDefinition for every entry in
    ``vm.vsphere_networks``,
  - a target StorageClass for every entry in ``vm.vsphere_datastores``.

If anything is missing, ``validate_plan_inputs`` returns the list of
gaps with VM-level detail. The caller turns that into a 422 so the
operator sees exactly which mapping rows need editing before they
press GENERATE again.

This is intentionally a pure-Python pre-check — the same logic the
MTV YAML emitter runs at download time, lifted earlier so plan
generation fails fast instead of producing a Plan row that can't
produce valid YAML.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.models.target import ResourceMapping
from app.models.vm import VM


@dataclass(frozen=True)
class MappingGap:
    """One missing mapping piece. ``kind`` is the resource category,
    ``source_value`` is the unmapped source name, ``vm_id`` / ``vm_name``
    identify a representative VM (the first the validator hit)."""

    vm_id: int
    vm_name: str
    kind: str  # "network" | "datastore" | "namespace"
    source_value: str

    def render(self) -> str:
        return (
            f"VM {self.vm_id} ({self.vm_name!r}): missing {self.kind} mapping "
            f"for source {self.source_value!r}"
        )


@dataclass
class ValidationResult:
    """Outcome of ``validate_plan_inputs``."""

    gaps: list[MappingGap] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.gaps

    def render(self, *, max_items: int = 10) -> str:
        if self.ok:
            return "Mapping coverage complete"
        lines = [g.render() for g in self.gaps[:max_items]]
        more = len(self.gaps) - max_items
        if more > 0:
            lines.append(f"… and {more} more")
        return "\n".join(lines)


def _mapped_networks(mapping: ResourceMapping | None) -> set[str]:
    if mapping is None:
        return set()
    out: set[str] = set()
    for entry in mapping.network_mappings or []:
        src = (entry or {}).get("source_network")
        tgt = ((entry or {}).get("target_network_name") or "").strip()
        if src and tgt:
            out.add(src)
    return out


def _mapped_datastores(mapping: ResourceMapping | None) -> set[str]:
    if mapping is None:
        return set()
    out: set[str] = set()
    for entry in mapping.storage_mappings or []:
        src = (entry or {}).get("source_datastore")
        tgt = ((entry or {}).get("target_storage_class") or "").strip()
        if src and tgt:
            out.add(src)
    return out


def _has_target_namespace(vm: VM, mapping: ResourceMapping | None) -> bool:
    """Either the VM carries an explicit target_namespace, or the
    mapping's namespace strategy resolves to something non-empty.

    The namespace strategy lives in ``mapping.namespace_mappings`` and
    can be either a list of criteria rows (legacy) or a dict (new
    NamespaceStrategy shape). We treat any non-empty list/dict as
    "has a strategy" — the YAML emitter does the final resolution.
    """
    if vm.target_namespace:
        return True
    if mapping is None:
        return False
    nm = mapping.namespace_mappings
    if isinstance(nm, dict) and nm:
        return True
    if isinstance(nm, list) and nm:
        return True
    return False


def _pick_mapping_for_vm(
    vm: VM,
    mappings: list[ResourceMapping],
) -> ResourceMapping | None:
    """Find the mapping whose ``vcenter_source_id`` matches ``vm``.

    Returns ``None`` if no mapping covers that vCenter. The caller
    decides whether a no-match is a coverage gap or fine (per-VM
    target fields might still resolve everything). VMs with no
    ``source_vcenter_id`` are handled separately by the validator
    via union-of-mappings, not by this lookup.
    """
    if vm.source_vcenter_id is None:
        return None
    for m in mappings:
        if m.vcenter_source_id == vm.source_vcenter_id:
            return m
    return None


def _has_target_namespace_via_any(vm: VM, mappings: list[ResourceMapping]) -> bool:
    """Namespace strategy can come from ANY selected mapping when the
    VM has no vcenter to pin it to."""
    if vm.target_namespace:
        return True
    for m in mappings:
        nm = m.namespace_mappings
        if isinstance(nm, dict) and nm:
            return True
        if isinstance(nm, list) and nm:
            return True
    return False


def validate_plan_inputs(
    vms: list[VM],
    mappings: list[ResourceMapping],
) -> ValidationResult:
    """Verify every VM has complete mapping coverage.

    Each VM is validated against the mapping whose
    ``vcenter_source_id`` matches the VM's source vCenter. When the
    operator has selected mappings (``mappings`` non-empty) but a VM's
    vCenter isn't covered by any of them, that's a ``no_mapping``
    gap surfaced as-is. When ``mappings`` is empty (operator opted
    out of mapping-driven coverage), every VM is validated as if
    ``mapping=None`` — only per-VM target fields count.

    VMs with no ``source_vcenter_id`` (test fixtures, legacy imports
    pre-dating the source_vcenter_id column) can't be routed by
    vcenter, so the validator falls back to the union of every
    selected mapping's coverage for those VMs. In production, the
    RVTools import always populates source_vcenter_id and the
    per-vcenter routing kicks in.
    """
    gaps: list[MappingGap] = []

    # Pre-compute the union once for the no-vcenter fallback path.
    all_nets: set[str] = set()
    all_ds: set[str] = set()
    for m in mappings:
        all_nets |= _mapped_networks(m)
        all_ds |= _mapped_datastores(m)

    for vm in vms:
        if vm.source_vcenter_id is None:
            # Can't route by vcenter — use the union of every selected
            # mapping's coverage. When mappings is empty both sets are
            # empty so this still produces per-resource gaps (matches
            # pre-multi-mapping ``mapping=None`` semantics).
            mapped_nets = all_nets
            mapped_ds = all_ds
            has_namespace = _has_target_namespace_via_any(vm, mappings)
        else:
            vm_mapping = _pick_mapping_for_vm(vm, mappings) if mappings else None
            if mappings and vm_mapping is None:
                gaps.append(
                    MappingGap(
                        vm_id=vm.id,
                        vm_name=vm.name,
                        kind="no_mapping",
                        source_value=(
                            f"no selected mapping covers source vcenter " f"{vm.source_vcenter_id}"
                        ),
                    )
                )
                continue
            mapped_nets = _mapped_networks(vm_mapping)
            mapped_ds = _mapped_datastores(vm_mapping)
            has_namespace = _has_target_namespace(vm, vm_mapping)

        # Networks
        for src in vm.vsphere_networks or []:
            if not src:
                continue
            if src in mapped_nets:
                continue
            if vm.target_network_attachment:
                # VM-level fallback present — emitter will use it.
                continue
            gaps.append(MappingGap(vm_id=vm.id, vm_name=vm.name, kind="network", source_value=src))

        # Datastores
        for src in vm.vsphere_datastores or []:
            if not src:
                continue
            if src in mapped_ds:
                continue
            if vm.target_storage_class:
                continue
            gaps.append(
                MappingGap(vm_id=vm.id, vm_name=vm.name, kind="datastore", source_value=src)
            )

        # Namespace
        if not has_namespace:
            gaps.append(
                MappingGap(
                    vm_id=vm.id,
                    vm_name=vm.name,
                    kind="namespace",
                    source_value="(no target namespace)",
                )
            )

    return ValidationResult(gaps=gaps)
