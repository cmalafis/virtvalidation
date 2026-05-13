"""Pure-Python chunking for hierarchical migration planning.

The planner used to feed the entire VM inventory into a single LLM
call. That doesn't scale: prompts overflow context windows past a few
hundred VMs, and the model's attention degrades long before that.

The hierarchical planner instead does the *partitioning* in Python and
asks the LLM only to reason about each small chunk. This module owns
the partitioning step. Inputs are deterministic; outputs are stable
across runs given the same input.

Three categories of dimensions drive partitioning:

  - **Mandatory** dimensions are hard partitions — VMs that differ on
    these MUST land in different chunks (different MTV providers,
    classification boundaries, etc.). Violations would generate
    invalid YAML or cross a federal boundary.

  - **Strong** dimensions are preferred sub-partitions — same chunk
    when possible, separate chunks when the resulting size makes the
    chunk easier to plan (a single application's prod and dev tiers
    rarely benefit from sharing wave reasoning).

  - **Soft** dimensions are LLM hints — passed through as context
    metadata but not used to partition. They help the per-chunk
    LLM call but the planner doesn't make grouping decisions on them.

Oversized chunks (> backend's max_planning_chunk_size) get subdivided
in this priority order: tier (web/app/db) → primary network → even
splits. Undersized chunks (< MIN_CHUNK_SIZE) get combined when their
mandatory dimensions match.

Dependency edges are inferred between chunks based on tier semantics:
DB before app before web within the same application; foundation
chunks (DNS/NTP/AD/PKI) before everything else.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Iterable, Optional

from app.models.plan import PlanningStrategy
from app.models.target import ResourceMapping
from app.models.vm import VM

logger = logging.getLogger(__name__)


# Below this floor, a single LLM call is simpler than chunking. The
# planner's small-input fallback uses this threshold too.
SINGLE_SHOT_THRESHOLD = 10

# Don't combine into anything smaller than this — preserves enough
# signal for the per-chunk LLM call to produce useful waves.
MIN_CHUNK_SIZE = 5

# Tier detection — applied to VM name patterns when subdividing
# oversized application chunks. Order matters: web/frontend tiers
# come last in dependency ordering so they're listed last.
_TIER_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("db", re.compile(r"\b(db|database|sql|postgres|oracle|mysql|mariadb|mongo)\b", re.I)),
    ("cache", re.compile(r"\b(redis|memcache|cache)\b", re.I)),
    ("queue", re.compile(r"\b(mq|kafka|rabbit|bus|broker)\b", re.I)),
    ("app", re.compile(r"\b(app|api|svc|service|backend|worker)\b", re.I)),
    ("web", re.compile(r"\b(web|www|nginx|apache|frontend|ui|portal)\b", re.I)),
    ("lb", re.compile(r"\b(lb|haproxy|loadbalancer|f5|ingress)\b", re.I)),
]

# Foundation services come before anything else in the dependency
# ordering. Detected by name + application_hint patterns.
_FOUNDATION_PATTERNS = re.compile(
    r"\b(dns|ntp|ad|active.?directory|ldap|pki|ca|cert|kms|vault|"
    r"radius|dhcp|tftp|syslog|sso|idp|identity)\b",
    re.I,
)


@dataclass
class Chunk:
    """One partition of the inventory.

    The chunk_id is a stable uuid generated at chunking time so the
    UI can render a "Chunk 1: Foundation Services" navigation that
    survives page reloads. The reason_for_chunk explains *why* this
    chunk exists — operators see it in the plan view's chunk
    breakdown.
    """

    chunk_id: str
    vm_ids: list[int]
    partition_key: dict
    sub_key: dict
    hints: dict
    reason_for_chunk: str
    sequence_dependencies: list[str] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.vm_ids)

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "vm_ids": list(self.vm_ids),
            "partition_key": dict(self.partition_key),
            "sub_key": dict(self.sub_key),
            "hints": dict(self.hints),
            "reason_for_chunk": self.reason_for_chunk,
            "sequence_dependencies": list(self.sequence_dependencies),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _detect_tier(name: str) -> str | None:
    """Return the first matching tier label for a VM name, or None."""
    if not name:
        return None
    for label, pat in _TIER_PATTERNS:
        if pat.search(name):
            return label
    return None


def _is_foundation(vm: VM) -> bool:
    if vm.application_hint and _FOUNDATION_PATTERNS.search(vm.application_hint):
        return True
    if vm.name and _FOUNDATION_PATTERNS.search(vm.name):
        return True
    if vm.role and _FOUNDATION_PATTERNS.search(vm.role):
        return True
    return False


def _primary_network(vm: VM) -> str | None:
    nets = vm.vsphere_networks or []
    return nets[0] if nets else None


def _classification_for(vm: VM, source_classification_by_id: dict[int, str]) -> str:
    """Resolve a VM's classification from its source vCenter row.

    The VM table doesn't carry classification directly — the field
    lives on VCenterSource and propagates by reference. Callers pass
    a precomputed lookup so the chunker stays pure.
    """
    if vm.source_vcenter_id is None:
        return "unclassified"
    return source_classification_by_id.get(vm.source_vcenter_id, "unclassified")


def _target_namespace_for(
    vm: VM,
    mappings: ResourceMapping | None,
    strategy: PlanningStrategy,
) -> str:
    """Resolve a VM's target namespace using the mapping rules.

    Falls back to the VM's per-row ``target_namespace`` (legacy column)
    or "default" when nothing is configured. Used as a hard partition:
    different namespaces never share a chunk.

    Routes through :class:`app.core.mtv.MappingResolver` so the
    new dict-shaped NamespaceStrategy and the legacy list-of-criteria
    rows resolve identically here and at MTV YAML generation.
    """
    if mappings is not None:
        from app.core.mtv import MappingResolver

        resolver = MappingResolver(
            namespace_mappings=mappings.namespace_mappings or [],
        )
        vm_payload = {
            "environment": vm.environment or "",
            "application_hint": vm.application_hint or "",
            "vcenter_folder": vm.vsphere_folder or "",
        }
        resolved = resolver.resolve_namespace(vm_payload)
        if resolved:
            return resolved
    return vm.target_namespace or "default"


def _partition_key(vm: VM, ctx: "_ChunkContext") -> tuple:
    """Return the hashable mandatory-dimension key for a VM."""
    return (
        vm.source_vcenter_id,
        ctx.target_cluster_id,  # one mapping → one target cluster
        _classification_for(vm, ctx.classification_by_vcenter),
        _target_namespace_for(vm, ctx.mappings, ctx.strategy),
    )


def _sub_key(vm: VM) -> tuple:
    """Strong-dimension key — preferred sub-partition within a partition."""
    return (
        (vm.application_hint or "").strip().lower() or "_unspecified_",
        (vm.environment or "").strip().lower() or "_unspecified_",
    )


def _hints_for(vms: list[VM]) -> dict:
    """Soft-dimension aggregate the LLM uses as context for the chunk."""
    owners: dict[str, int] = {}
    os_families: dict[str, int] = {}
    networks: dict[str, int] = {}
    for vm in vms:
        if vm.owner:
            owners[vm.owner] = owners.get(vm.owner, 0) + 1
        if vm.os_family:
            os_families[vm.os_family] = os_families.get(vm.os_family, 0) + 1
        for n in vm.vsphere_networks or []:
            networks[n] = networks.get(n, 0) + 1
    return {
        "owners": dict(sorted(owners.items(), key=lambda kv: -kv[1])),
        "os_families": dict(sorted(os_families.items(), key=lambda kv: -kv[1])),
        "primary_networks": dict(sorted(networks.items(), key=lambda kv: -kv[1])),
    }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
@dataclass
class _ChunkContext:
    """Internal carry-along so helpers don't take 6 args each."""

    strategy: PlanningStrategy
    mappings: Optional[ResourceMapping]
    target_cluster_id: Optional[int]
    classification_by_vcenter: dict[int, str]
    max_size: int


def chunk_vms(
    vms: list[VM],
    *,
    mappings: Optional[ResourceMapping] = None,
    strategy: PlanningStrategy,
    max_size: int,
    classification_by_vcenter: Optional[dict[int, str]] = None,
) -> list[Chunk]:
    """Pure-Python chunker. Returns an ordered list of :class:`Chunk`
    instances ready to feed into per-chunk LLM planning.

    Args:
        vms: every VM in scope. Empty list returns empty list.
        mappings: optional :class:`ResourceMapping` — supplies the
            target namespace rules and target cluster id.
        strategy: the operator's planning strategy. The chunker reads
            very little from this directly today (the LLM consumes
            the full strategy on the per-chunk call) but it's threaded
            through for future-proofing.
        max_size: the active backend's ``max_planning_chunk_size``.
            Chunks larger than this get subdivided.
        classification_by_vcenter: ``{vcenter_id: "unclassified" | ...}``
            lookup. The VM model doesn't carry classification directly,
            so callers must precompute this from
            :class:`VCenterSource` rows.

    Returns:
        Chunks in dependency-friendly order: foundation chunks first,
        then by partition key, then by tier within an application.
    """
    if not vms:
        return []

    ctx = _ChunkContext(
        strategy=strategy,
        mappings=mappings,
        target_cluster_id=(mappings.ocp_target_id if mappings is not None else None),
        classification_by_vcenter=classification_by_vcenter or {},
        max_size=max(1, int(max_size)),
    )

    # 1. Hard partition by mandatory dimensions.
    hard_groups: dict[tuple, list[VM]] = {}
    for vm in vms:
        key = _partition_key(vm, ctx)
        hard_groups.setdefault(key, []).append(vm)

    # 2. Sub-partition by strong dimensions within each hard partition.
    chunks: list[Chunk] = []
    for hard_key, group in hard_groups.items():
        sub_groups: dict[tuple, list[VM]] = {}
        for vm in group:
            sub_groups.setdefault(_sub_key(vm), []).append(vm)
        for sub_key, sub_group in sub_groups.items():
            chunks.extend(_emerge_chunks_for(sub_group, hard_key, sub_key, ctx))

    # 3. Combine undersized chunks where compatible.
    chunks = _combine_undersized(chunks)

    # 4. Detect cross-chunk dependencies.
    chunks = _attach_dependencies(chunks)

    # 5. Order: foundation first, then by partition key + dependency.
    chunks.sort(key=_ordering_key)

    return chunks


def _emerge_chunks_for(
    vms: list[VM],
    hard_key: tuple,
    sub_key_tuple: tuple,
    ctx: _ChunkContext,
) -> list[Chunk]:
    """Emit one or more chunks from a (hard, sub) group, subdividing
    when the group exceeds ``max_size``.

    Subdivision priority: tier → primary network → even split.
    """
    partition_dict = _explode_partition_key(hard_key)
    sub_dict = {
        "application_hint": sub_key_tuple[0],
        "environment": sub_key_tuple[1],
    }

    if len(vms) <= ctx.max_size:
        return [_make_chunk(vms, partition_dict, sub_dict, reason=None)]

    # Try tier subdivision first.
    by_tier: dict[str, list[VM]] = {}
    untiered: list[VM] = []
    for vm in vms:
        tier = _detect_tier(vm.name or "") or _detect_tier(vm.role or "")
        if tier is None:
            untiered.append(vm)
        else:
            by_tier.setdefault(tier, []).append(vm)

    if by_tier:
        emitted: list[Chunk] = []
        # Tiered VMs: one chunk per tier, recursively subdivide if a
        # tier still exceeds max_size.
        for tier, members in by_tier.items():
            sub_with_tier = {**sub_dict, "tier": tier}
            if len(members) <= ctx.max_size:
                emitted.append(
                    _make_chunk(
                        members,
                        partition_dict,
                        sub_with_tier,
                        reason=f"tier={tier}",
                    )
                )
            else:
                emitted.extend(
                    _split_by_network_or_evenly(
                        members, partition_dict, sub_with_tier, ctx.max_size
                    )
                )
        if untiered:
            # VMs that didn't match any tier ride together — could be
            # supporting infra inside the same application.
            sub_with_tier = {**sub_dict, "tier": "_misc_"}
            if len(untiered) <= ctx.max_size:
                emitted.append(
                    _make_chunk(
                        untiered,
                        partition_dict,
                        sub_with_tier,
                        reason="tier=misc (no tier match)",
                    )
                )
            else:
                emitted.extend(
                    _split_by_network_or_evenly(
                        untiered, partition_dict, sub_with_tier, ctx.max_size
                    )
                )
        return emitted

    # No tier signal at all — drop straight to network/even split.
    return _split_by_network_or_evenly(vms, partition_dict, sub_dict, ctx.max_size)


def _split_by_network_or_evenly(
    vms: list[VM],
    partition_dict: dict,
    sub_dict: dict,
    max_size: int,
) -> list[Chunk]:
    by_net: dict[str, list[VM]] = {}
    for vm in vms:
        net = _primary_network(vm) or "_no_network_"
        by_net.setdefault(net, []).append(vm)

    if len(by_net) > 1:
        emitted: list[Chunk] = []
        for net, members in by_net.items():
            sub_with_net = {**sub_dict, "primary_network": net}
            if len(members) <= max_size:
                emitted.append(
                    _make_chunk(
                        members,
                        partition_dict,
                        sub_with_net,
                        reason=f"primary_network={net}",
                    )
                )
            else:
                emitted.extend(_even_splits(members, partition_dict, sub_with_net, max_size))
        return emitted

    return _even_splits(vms, partition_dict, sub_dict, max_size)


def _even_splits(
    vms: list[VM],
    partition_dict: dict,
    sub_dict: dict,
    max_size: int,
) -> list[Chunk]:
    """Last-resort: chop into max_size-sized slices."""
    chunks: list[Chunk] = []
    for i in range(0, len(vms), max_size):
        slice_ = vms[i : i + max_size]
        sub_with_slice = {**sub_dict, "split_index": i // max_size}
        chunks.append(
            _make_chunk(
                slice_,
                partition_dict,
                sub_with_slice,
                reason=f"even split ({len(slice_)} VMs)",
            )
        )
    return chunks


def _make_chunk(
    vms: list[VM],
    partition_dict: dict,
    sub_dict: dict,
    *,
    reason: str | None,
) -> Chunk:
    is_foundation = any(_is_foundation(vm) for vm in vms)
    label = _chunk_label(vms, partition_dict, sub_dict, foundation=is_foundation)
    final_reason = reason or _default_reason(sub_dict, foundation=is_foundation)
    return Chunk(
        chunk_id=str(uuid.uuid4()),
        vm_ids=[vm.id for vm in vms],
        partition_key=dict(partition_dict),
        sub_key={**sub_dict, "label": label, "is_foundation": is_foundation},
        hints=_hints_for(vms),
        reason_for_chunk=final_reason,
    )


def _explode_partition_key(key: tuple) -> dict:
    return {
        "source_vcenter_id": key[0],
        "target_ocp_cluster_id": key[1],
        "classification_level": key[2],
        "target_namespace": key[3],
    }


def _chunk_label(vms: list[VM], partition_dict: dict, sub_dict: dict, *, foundation: bool) -> str:
    """Human-readable label the UI surfaces in the chunk navigation."""
    if foundation:
        return "Foundation Services"
    app = sub_dict.get("application_hint") or ""
    env = sub_dict.get("environment") or ""
    tier = sub_dict.get("tier") or ""
    if app and app != "_unspecified_":
        title = app.replace("-", " ").title()
        if env and env != "_unspecified_":
            title += f" ({env})"
        if tier and tier != "_misc_":
            title += f" — {tier} tier"
        return title
    if env and env != "_unspecified_":
        return env.title() + " VMs"
    return f"Mixed inventory ({len(vms)} VMs)"


def _default_reason(sub_dict: dict, *, foundation: bool) -> str:
    if foundation:
        return "Foundation services (DNS / NTP / identity)"
    parts = []
    if sub_dict.get("application_hint") and sub_dict["application_hint"] != "_unspecified_":
        parts.append(f"application={sub_dict['application_hint']}")
    if sub_dict.get("environment") and sub_dict["environment"] != "_unspecified_":
        parts.append(f"environment={sub_dict['environment']}")
    if not parts:
        return "Inventory partition (no application or environment hints)"
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Combine undersized chunks
# ---------------------------------------------------------------------------
def _combine_undersized(chunks: list[Chunk]) -> list[Chunk]:
    """Merge chunks below MIN_CHUNK_SIZE into compatible siblings.

    Combining preserves the structural semantics the planner depends
    on:

      - **Foundation chunks** combine only with other foundations
        (and the merged chunk keeps ``is_foundation=True``). The
        planner's foundation-first ordering is load-bearing — losing
        the flag would re-sequence everything wrong.

      - **Tiered chunks** (``sub_key.tier`` set) never combine with
        non-tier siblings. db/app/web tiers carry dependency
        semantics; collapsing them defeats the per-tier ordering.

      - Otherwise, compatible = same ``partition_key`` (federal
        classification + namespace boundaries kept intact).
    """
    out: list[Chunk] = []
    # Carve foundations first — they merge among themselves regardless
    # of partition because there's typically only one set of foundation
    # services in scope.
    foundation_groups: dict[tuple, list[Chunk]] = {}
    by_partition: dict[tuple, list[Chunk]] = {}
    for c in chunks:
        if c.sub_key.get("is_foundation"):
            foundation_groups.setdefault(_partition_tuple(c.partition_key), []).append(c)
        else:
            by_partition.setdefault(_partition_tuple(c.partition_key), []).append(c)

    # Merge foundations within each partition. Even a single foundation
    # chunk with 2 VMs stays as-is (it's already meaningful).
    for _, group in foundation_groups.items():
        small = [c for c in group if c.size < MIN_CHUNK_SIZE]
        big = [c for c in group if c.size >= MIN_CHUNK_SIZE]
        out.extend(big)
        if not small:
            continue
        merged = _flatten_foundation(small)
        if merged is not None:
            out.append(merged)

    for _, group in by_partition.items():
        # Tiered chunks pass through untouched — small db tier (3 VMs)
        # still represents real dependency structure.
        tiered = [c for c in group if c.sub_key.get("tier")]
        out.extend(tiered)

        candidates = [c for c in group if not c.sub_key.get("tier")]
        small = [c for c in candidates if c.size < MIN_CHUNK_SIZE]
        big = [c for c in candidates if c.size >= MIN_CHUNK_SIZE]
        out.extend(big)
        if not small:
            continue

        # Merge until each merged chunk reaches MIN_CHUNK_SIZE or we
        # run out of donors. We don't try to be clever about which to
        # merge first — applications are already similar-by-partition
        # by definition.
        current_vms: list[int] = []
        sub_apps: list[str] = []
        for c in sorted(small, key=lambda c: c.size):
            current_vms.extend(c.vm_ids)
            label = c.sub_key.get("label") or c.sub_key.get("application_hint") or ""
            if label and label not in sub_apps:
                sub_apps.append(label)
            if len(current_vms) >= MIN_CHUNK_SIZE:
                out.append(
                    _make_combined(
                        current_vms,
                        small[0].partition_key,
                        sub_apps,
                    )
                )
                current_vms = []
                sub_apps = []
        if current_vms:
            # Trailing leftover: ship as its own small chunk rather
            # than dropping. The per-chunk LLM call still works for
            # 2-3 VMs; the planner just gets an extra small chunk.
            out.append(_make_combined(current_vms, small[0].partition_key, sub_apps))
    return out


def _flatten_foundation(chunks: list[Chunk]) -> Chunk | None:
    """Merge a list of small foundation chunks into one, preserving
    the foundation flag so dependency attachment still treats it as
    the planner's anchor."""
    if not chunks:
        return None
    if len(chunks) == 1:
        return chunks[0]
    vm_ids: list[int] = []
    apps: list[str] = []
    for c in chunks:
        vm_ids.extend(c.vm_ids)
        label = c.sub_key.get("application_hint") or ""
        if label and label not in apps:
            apps.append(label)
    return Chunk(
        chunk_id=str(uuid.uuid4()),
        vm_ids=list(vm_ids),
        partition_key=dict(chunks[0].partition_key),
        sub_key={
            "label": "Foundation Services",
            "is_foundation": True,
            "combined": True,
            "applications": apps,
        },
        hints={"reason": "merged foundation siblings"},
        reason_for_chunk=(
            "Foundation services (DNS / NTP / identity) merged into a "
            "single chunk so the planner sequences them as one dependency "
            "anchor for the remaining application chunks."
        ),
    )


def _make_combined(vm_ids: list[int], partition_key: dict, sub_apps: list[str]) -> Chunk:
    label = " + ".join(sub_apps[:3])
    if len(sub_apps) > 3:
        label += f" + {len(sub_apps) - 3} more"
    if not label:
        label = "Combined small chunks"
    return Chunk(
        chunk_id=str(uuid.uuid4()),
        vm_ids=list(vm_ids),
        partition_key=dict(partition_key),
        sub_key={
            "label": label,
            "is_foundation": False,
            "combined": True,
            "applications": sub_apps,
        },
        hints={"reason": "combined undersized siblings"},
        reason_for_chunk=(
            f"Combined {len(sub_apps)} small applications "
            f"({', '.join(sub_apps[:5])}) sharing the same scope; "
            "individual chunks would have been too small to plan usefully."
        ),
    )


def _partition_tuple(d: dict) -> tuple:
    return (
        d.get("source_vcenter_id"),
        d.get("target_ocp_cluster_id"),
        d.get("classification_level"),
        d.get("target_namespace"),
    )


# ---------------------------------------------------------------------------
# Dependency detection
# ---------------------------------------------------------------------------
def _attach_dependencies(chunks: list[Chunk]) -> list[Chunk]:
    """Set ``sequence_dependencies`` on each chunk based on tier
    semantics and foundation-first ordering.

    The rules are:

      - Every non-foundation chunk depends on every foundation chunk.
      - Within the same application, db tier → app tier → web tier.
      - Production chunks depend on their non-prod siblings (same
        application, environment != prod).
    """
    foundations = [c for c in chunks if c.sub_key.get("is_foundation")]
    foundation_ids = [c.chunk_id for c in foundations]

    # Index by (app, env, tier) for quick tier-dependency lookup.
    by_app_env: dict[tuple[str, str], list[Chunk]] = {}
    for c in chunks:
        app = c.sub_key.get("application_hint") or "_combined_"
        env = c.sub_key.get("environment") or "_any_"
        by_app_env.setdefault((app, env), []).append(c)

    tier_order = {"db": 0, "cache": 1, "queue": 1, "app": 2, "lb": 3, "web": 4}

    for c in chunks:
        deps = list(c.sub_key.get("sequence_dependencies") or [])
        if not c.sub_key.get("is_foundation"):
            for fid in foundation_ids:
                if fid not in deps:
                    deps.append(fid)

        # Tier dependency: lower tier_order → earlier wave block.
        app = c.sub_key.get("application_hint") or "_combined_"
        env = c.sub_key.get("environment") or "_any_"
        tier = c.sub_key.get("tier")
        if tier and tier in tier_order:
            siblings = by_app_env.get((app, env), [])
            for s in siblings:
                stier = s.sub_key.get("tier")
                if stier and stier in tier_order and tier_order[stier] < tier_order[tier]:
                    if s.chunk_id != c.chunk_id and s.chunk_id not in deps:
                        deps.append(s.chunk_id)

        # Prod-after-non-prod within the same application.
        if env in {"prod", "production"} and app != "_unspecified_":
            for (other_app, other_env), members in by_app_env.items():
                if other_app != app:
                    continue
                if other_env in {"prod", "production"}:
                    continue
                for s in members:
                    if s.chunk_id != c.chunk_id and s.chunk_id not in deps:
                        deps.append(s.chunk_id)

        c.sequence_dependencies = deps
    return chunks


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------
def _ordering_key(c: Chunk) -> tuple:
    """Sort chunks foundations-first, then by partition + tier."""
    foundation = 0 if c.sub_key.get("is_foundation") else 1
    partition = (
        c.partition_key.get("classification_level", ""),
        str(c.partition_key.get("source_vcenter_id") or ""),
        str(c.partition_key.get("target_ocp_cluster_id") or ""),
        c.partition_key.get("target_namespace", ""),
    )
    env = c.sub_key.get("environment") or ""
    env_rank = 0 if env not in {"prod", "production"} else 1
    tier_order = {"db": 0, "cache": 1, "queue": 1, "app": 2, "lb": 3, "web": 4, "_misc_": 9}
    tier_rank = tier_order.get(c.sub_key.get("tier") or "", 5)
    label = c.sub_key.get("label") or ""
    return (
        foundation,
        partition,
        env_rank,
        c.sub_key.get("application_hint") or "",
        tier_rank,
        label,
    )


# ---------------------------------------------------------------------------
# Validation helpers (used by the planner orchestrator + tests)
# ---------------------------------------------------------------------------
def validate_chunks(chunks: Iterable[Chunk], expected_vm_ids: set[int]) -> None:
    """Raise AssertionError if the chunk set has an integrity issue.

    Used by the planner to fail fast before kicking off LLM calls
    against a corrupt chunk set, and by tests to assert correctness
    in fixtures.
    """
    seen: set[int] = set()
    for c in chunks:
        if not c.vm_ids:
            raise AssertionError(f"Chunk {c.chunk_id} has no VMs")
        for vm_id in c.vm_ids:
            if vm_id in seen:
                raise AssertionError(f"vm_id {vm_id} appears in multiple chunks")
            seen.add(vm_id)
    missing = expected_vm_ids - seen
    if missing:
        raise AssertionError(f"Chunks miss vm_ids: {sorted(missing)[:10]}")
    extra = seen - expected_vm_ids
    if extra:
        raise AssertionError(f"Chunks contain unknown vm_ids: {sorted(extra)[:10]}")
