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


def validate_plan_inputs(
    vms: list[VM],
    mapping: ResourceMapping | None,
) -> ValidationResult:
    """Verify every VM has complete mapping coverage.

    The validator walks each VM's source resources and confirms a
    target exists either in the mapping payload or as a per-VM field.
    Gaps are collected, not raised — the caller decides whether to
    surface as 422 or to log + continue.
    """
    mapped_nets = _mapped_networks(mapping)
    mapped_ds = _mapped_datastores(mapping)
    gaps: list[MappingGap] = []

    for vm in vms:
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
        if not _has_target_namespace(vm, mapping):
            gaps.append(
                MappingGap(
                    vm_id=vm.id,
                    vm_name=vm.name,
                    kind="namespace",
                    source_value="(no target namespace)",
                )
            )

    return ValidationResult(gaps=gaps)
