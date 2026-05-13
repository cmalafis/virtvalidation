"""Mechanical wave assignment — pure Python, deterministic, fast.

The previous architecture had the LLM see the whole group list and
decide the wave structure in one shot. That broke at scale: 57
groups blew past Granite's reliable output window, the planner
retried + fell back to a deterministic assigner anyway. The
mechanical path was always the load-bearing one; this module
makes it the ONLY path for wave structure decisions.

What this module owns:

  - **Wave count + ordering**. Topological sort over
    ``dependency_hints`` + role priority (infrastructure → data →
    app → web → edge). No LLM involvement, no retry needed,
    deterministic across runs.
  - **Hard limits enforcement** that match MTV operational
    reality:
      * ``MAX_VMS_PER_WAVE`` — MTV throttles concurrent migrations
        per source ESXi to ~10. Waves wider than this would get
        serialized anyway, so we don't pretend otherwise.
      * ``MAX_HA_PEERS_PER_WAVE`` — never lose quorum on an
        infrastructure service mid-wave. Two AD-DC primaries
        moving together is allowed; three at once is not.
      * **Partition coherence** — every wave maps 1:1 to a single
        MTV ``Plan`` CR, which can only point at one source provider
        (one source vCenter) and one ``spec.targetNamespace``.
        Co-packing groups across either boundary would emit YAML
        that fails at ``oc apply``. The preclassifier already keys
        groups by ``(vcenter_id, target_namespace)``; the wave
        packer enforces the boundary by refusing to place a group
        into a wave whose existing groups disagree on either field.
        Multiple network / storage mapping rows per wave ARE
        expected (one ``Plan`` typically maps many vSphere
        portgroups onto target NADs) — those live inside the single
        NetworkMap / StorageMap CR the emitter builds.
  - **Group splitting** when a single group exceeds
    ``MAX_VMS_PER_WAVE``. The oversized group becomes ``/batch-1``,
    ``/batch-2``, … sub-groups, each ≤ the limit, slotted into
    consecutive waves.
  - **HA family detection** across groups so AD-DC peers detected
    in distinct preclassified groups still get spread across waves.

What the LLM does (separately, see ``llm_wave_rationale.py``):
generates per-wave rationale text on the small (≤10 group) wave
slice. The LLM does NOT influence which groups land in which wave.

Failure modes:
  - This module never raises on a well-formed input. Cycles in
    ``dependency_hints`` are resolved by appending remainder in
    stable id order (same approach the previous topological-sort
    fallback used).
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from app.core.config import settings as _module_settings
from app.core.preclassifier import GroupKey, VMGroup

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Hard limits
# ---------------------------------------------------------------------------
#: Max VMs that can land in a single wave. MTV's Forklift controller
#: throttles concurrent inventory copy operations per source ESXi to
#: around 10; making waves wider than this is a paper exercise the
#: scheduler ignores. Federal customers' tabletop reviews accept this
#: number as the practical migration concurrency. Override via the
#: ``MAX_VMS_PER_WAVE`` env var only with explicit operator approval.
MAX_VMS_PER_WAVE: int = 10

#: Max HA peers from the same family that may share a wave. Two is
#: the federal-customer compromise: pairs of AD-DC primaries are
#: acceptable when the cluster runs ≥3 nodes (quorum survives), but
#: three at once would risk quorum loss. Setting this higher requires
#: federal compliance sign-off — see ``docs/HA_MIGRATION_STRATEGY.md``.
MAX_HA_PEERS_PER_WAVE: int = 2


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------
@dataclass
class Wave:
    """One migration wave — sequence number + ordered groups inside.

    Construction is deterministic: the ``MechanicalWaveAssigner``
    always produces the same Wave list from the same input groups.
    Federal audit trails reconstruct wave membership months later
    by re-running this code against the snapshot inventory.
    """

    wave_number: int
    groups: list[VMGroup] = field(default_factory=list)
    estimated_risk: str = "low"
    notes: list[str] = field(default_factory=list)

    @property
    def vm_ids(self) -> list[int]:
        out: list[int] = []
        for g in self.groups:
            out.extend(g.vm_ids)
        return out

    @property
    def vm_count(self) -> int:
        return sum(len(g.vm_ids) for g in self.groups)


# ---------------------------------------------------------------------------
# HA family detection across groups
# ---------------------------------------------------------------------------
_HA_NAME_STRIP_RE = re.compile(r"[-_]?\d+(?:\.[a-z0-9.-]+)?$", re.IGNORECASE)


def _ha_family_key(group: VMGroup) -> str | None:
    """Compute a family key that identifies HA peers across groups.

    Two HA-tagged groups land in the same family when they share
    the same role + application_hint + parent-discriminator stem.
    Returns ``None`` when the group has no HA tags — those groups
    never participate in spreading.

    For micro-groups produced by ``PreClassifier.split_ha_group``
    (discriminator format ``<parent>/ha:<role>:<vm_name>``), the
    parent discriminator carries the family identity — primary and
    replica micro-groups MUST share a family key so the wave skeleton
    places them in different waves. We slice off the trailing
    ``/ha:...`` suffix to recover the parent before computing the
    name stem.

    Example: micro-groups
      ``vc1/prod/data/stateful/hint:app1:data/ha:primary:postgres-primary``
      ``vc1/prod/data/stateful/hint:app1:data/ha:replica:postgres-replica-01``
    both stem to the parent ``...hint:app1:data`` and share the
    family key ``data/app1/hint:app1:data``.
    """
    ha_count = sum(1 for m in group.ha_members if m.ha_role != "standalone")
    if ha_count == 0:
        return None

    discriminator = group.key.discriminator
    parent_discriminator = discriminator
    ha_idx = discriminator.find("/ha:")
    if ha_idx >= 0:
        parent_discriminator = discriminator[:ha_idx]

    # When no parent context exists (single-VM group, not a split),
    # fall back to the sample VM's name stem.
    if "/ha:" in discriminator:
        stem = parent_discriminator
    else:
        sample_name = next(
            (m.vm_name for m in group.ha_members if m.ha_role != "standalone"),
            "",
        )
        stem = _HA_NAME_STRIP_RE.sub("", sample_name).lower() or "unknown"

    hints = group.shared_attributes.get("application_hints", [])
    app = hints[0] if hints else "unknown"
    return f"{group.estimated_role}/{app}/{stem}"


# ---------------------------------------------------------------------------
# Assigner
# ---------------------------------------------------------------------------
_ROLE_PRIORITY = {
    "infrastructure": 0,
    "data": 1,
    "app": 2,
    "web": 3,
    "edge": 4,
    "other": 5,
}
_RISK_PRIORITY = {"low": 0, "medium": 1, "high": 2}


class MechanicalWaveAssigner:
    """Build wave structure from preclassified groups, no LLM.

    Construction is cheap; reuse across calls is safe (no instance
    state mutated outside ``assign_waves``).
    """

    def __init__(
        self,
        *,
        max_vms_per_wave: int = MAX_VMS_PER_WAVE,
        max_ha_peers_per_wave: int = MAX_HA_PEERS_PER_WAVE,
        items_per_llm_call: int | None = None,
    ) -> None:
        self.max_vms_per_wave = max(1, int(max_vms_per_wave))
        self.max_ha_peers_per_wave = max(1, int(max_ha_peers_per_wave))
        # The per-LLM-call ceiling — used later by the rationale path
        # to slice a wave into LLM-friendly chunks if it ends up
        # carrying more than this many groups. For wave assignment
        # itself, only the VM count limit matters.
        self.items_per_llm_call = (
            items_per_llm_call
            if items_per_llm_call is not None
            else _module_settings.llm_max_items_per_call
        )

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def assign_waves(self, groups: list[VMGroup]) -> list[Wave]:
        if not groups:
            return []

        # 1. Split oversized groups into batch sub-groups so no
        #    single group exceeds MAX_VMS_PER_WAVE.
        prepared = self._split_oversized_groups(groups)

        # 2. Topologically sort so dependencies always come before
        #    their dependents.
        ordered = self._topological_sort(prepared)

        # 3. Detect HA families so we can spread peers across waves.
        family_to_group_ids = self._detect_ha_families(ordered)
        group_to_family = {gid: fam for fam, gids in family_to_group_ids.items() for gid in gids}

        # 4. Greedy wave packing: walk groups in dependency order +
        #    role priority, packing each into the earliest wave that
        #    (a) satisfies its dependencies, (b) has room under the
        #    MAX_VMS_PER_WAVE limit, and (c) has < MAX_HA_PEERS_PER_WAVE
        #    members of the same HA family.
        wave_for_group: dict[str, int] = {}
        waves: dict[int, Wave] = {}
        family_per_wave: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

        for group in ordered:
            min_wave_from_deps = self._min_wave_from_deps(group, wave_for_group)
            family = group_to_family.get(group.id)
            target = self._find_target_wave(
                group,
                waves,
                family_per_wave,
                min_wave_from_deps,
                family,
            )
            wave_for_group[group.id] = target
            wave = waves.setdefault(target, Wave(wave_number=target))
            wave.groups.append(group)
            if family is not None:
                ha_count = sum(1 for m in group.ha_members if m.ha_role != "standalone")
                # When the placed group is a primary, lock out every
                # other peer from this wave by bumping the family
                # counter to the per-wave cap. Replicas + members
                # of the same family will see the wave as "full"
                # and find a later one.
                if any(m.ha_role == "primary" for m in group.ha_members):
                    family_per_wave[target][family] = self.max_ha_peers_per_wave
                else:
                    family_per_wave[target][family] += ha_count

        # 5. Sort + renumber waves so they're 1..N contiguous and the
        #    contents within each wave have a deterministic order
        #    (role priority, then risk priority, then group id).
        return self._finalize(waves)

    # ------------------------------------------------------------------
    # Splitting
    # ------------------------------------------------------------------
    def _split_oversized_groups(self, groups: list[VMGroup]) -> list[VMGroup]:
        out: list[VMGroup] = []
        for g in groups:
            if len(g.vm_ids) <= self.max_vms_per_wave:
                out.append(g)
                continue
            out.extend(self._split_one(g))
        return out

    def _split_one(self, group: VMGroup) -> list[VMGroup]:
        """Split a single oversized group into batch sub-groups.

        VMs are partitioned in sorted-id order so the same input
        always produces the same batch membership. Each batch
        inherits the parent's risk, dependencies, and shared
        attributes — only ``vm_ids`` and ``notes`` differ.
        """
        sorted_ids = sorted(group.vm_ids)
        id_to_member = {m.vm_id: m for m in group.ha_members}
        batches: list[VMGroup] = []
        for batch_num, start in enumerate(
            range(0, len(sorted_ids), self.max_vms_per_wave),
            start=1,
        ):
            batch_ids = sorted_ids[start : start + self.max_vms_per_wave]
            batch_members = [id_to_member[i] for i in batch_ids if i in id_to_member]
            sub = VMGroup(
                key=GroupKey(
                    vcenter_id=group.key.vcenter_id,
                    target_namespace=group.key.target_namespace,
                    role=group.key.role,
                    state=group.key.state,
                    discriminator=f"{group.key.discriminator}/batch-{batch_num}",
                ),
                vm_ids=batch_ids,
                shared_attributes=dict(group.shared_attributes),
                estimated_role=group.estimated_role,
                estimated_state=group.estimated_state,
                migration_risk=group.migration_risk,
                dependency_hints=list(group.dependency_hints),
                notes=(
                    f"Batch {batch_num} of {group.id} "
                    f"({len(batch_ids)} VMs) — split for MTV per-ESXi "
                    "concurrency limit"
                ),
                risk_assessment=group.risk_assessment,
                ha_members=batch_members,
            )
            batches.append(sub)
        return batches

    # ------------------------------------------------------------------
    # Topological sort over dependency_hints
    # ------------------------------------------------------------------
    @staticmethod
    def _topological_sort(groups: list[VMGroup]) -> list[VMGroup]:
        """Kahn's algorithm, stable on id ties. Cycles are tolerated
        by appending the remainder in id order — dependency_hints is
        advisory, not a hard constraint.
        """
        by_id = {g.id: g for g in groups}
        indegree: dict[str, int] = {g.id: 0 for g in groups}
        for g in groups:
            for dep in g.dependency_hints:
                if dep in indegree:
                    indegree[g.id] += 1
        ready = sorted(gid for gid, deg in indegree.items() if deg == 0)
        ordered: list[VMGroup] = []
        while ready:
            ready.sort()
            gid = ready.pop(0)
            ordered.append(by_id[gid])
            for g in groups:
                if gid in g.dependency_hints and indegree[g.id] > 0:
                    indegree[g.id] -= 1
                    if indegree[g.id] == 0:
                        ready.append(g.id)
        placed = {g.id for g in ordered}
        for gid in sorted(by_id):
            if gid not in placed:
                ordered.append(by_id[gid])

        # Within the topo order, prefer higher-priority roles + lower
        # risk first. This biases earlier waves toward the federal-
        # customer rollout pattern: infrastructure → data → app → web.
        # Stable sort preserves dependency order when role/risk tie.
        return sorted(
            ordered,
            key=lambda g: (
                _ROLE_PRIORITY.get(g.estimated_role, 99),
                _RISK_PRIORITY.get(g.migration_risk, 1),
                g.id,
            ),
        )

    # ------------------------------------------------------------------
    # HA family detection
    # ------------------------------------------------------------------
    @staticmethod
    def _detect_ha_families(groups: list[VMGroup]) -> dict[str, list[str]]:
        """Build {family_key: [group_id, ...]} for spreading."""
        out: dict[str, list[str]] = defaultdict(list)
        for g in groups:
            family = _ha_family_key(g)
            if family is not None:
                out[family].append(g.id)
        return {k: v for k, v in out.items() if len(v) > 1}

    # ------------------------------------------------------------------
    # Wave packing
    # ------------------------------------------------------------------
    @staticmethod
    def _min_wave_from_deps(
        group: VMGroup,
        wave_for_group: dict[str, int],
    ) -> int:
        """Earliest wave that comes after every dependency."""
        min_wave = 1
        for dep in group.dependency_hints:
            placed = wave_for_group.get(dep)
            if placed is not None:
                min_wave = max(min_wave, placed + 1)
        return min_wave

    def _find_target_wave(
        self,
        group: VMGroup,
        waves: dict[int, Wave],
        family_per_wave: dict[int, dict[str, int]],
        min_wave: int,
        family: str | None,
    ) -> int:
        """Walk wave numbers from ``min_wave`` upward until we find
        one that has room for this group's VMs AND fewer than
        ``max_ha_peers_per_wave`` peers of its HA family already
        placed. Creates a new wave if none of the existing waves fit.
        """
        ha_in_group = sum(1 for m in group.ha_members if m.ha_role != "standalone")
        # A primary's wave gets a tighter cap of 1 — primaries
        # never share a wave with another peer from their family.
        # This honors the spec's "Primary first, then replicas
        # later" ordering: primary alone in wave N, replicas
        # follow in N+1, N+2.
        is_primary_group = any(m.ha_role == "primary" for m in group.ha_members)
        effective_family_cap = 1 if is_primary_group else self.max_ha_peers_per_wave
        # Defensive: when a single group's intrinsic HA peer count
        # exceeds the per-wave family cap, no wave will ever satisfy
        # the family constraint and the search would loop forever.
        # This case shouldn't happen in practice — ``ha_strategy=spread``
        # in plan_with_groups splits multi-member HA groups into
        # singletons before the skeleton sees them. But unit tests
        # that call ``assign_waves`` directly without going through
        # the HA spread step would hit this. Disable the family check
        # for the group's own first placement when its HA count
        # already exceeds the cap — the spread guarantee is the
        # caller's responsibility, not the assigner's.
        enforce_family = family is not None and ha_in_group <= effective_family_cap
        candidate = min_wave
        # Iteration upper bound — fresh waves always succeed, so the
        # cap is "however many existing waves + 1". Anything above
        # that is a logic bug; ``max_waves`` lets us raise instead of
        # hang.
        max_waves = max(min_wave + len(waves) + 2, 1024)
        while candidate <= max_waves:
            existing = waves.get(candidate)
            # Partition coherence: each wave emits exactly one MTV
            # ``Plan`` CR, which can carry only one source provider
            # + one target namespace. The preclassifier already keys
            # groups by ``(vcenter_id, target_namespace)``, so we
            # just refuse to place this group into a non-empty wave
            # whose first group disagrees on either dimension. A
            # fresh wave (no ``existing`` yet) seeds whichever
            # partition this group declares.
            if existing and existing.groups:
                seed_key = existing.groups[0].key
                if (
                    seed_key.vcenter_id != group.key.vcenter_id
                    or seed_key.target_namespace != group.key.target_namespace
                ):
                    candidate += 1
                    continue
            current_vms = existing.vm_count if existing else 0
            current_family = family_per_wave[candidate].get(family, 0) if enforce_family else 0
            fits_vms = current_vms + len(group.vm_ids) <= self.max_vms_per_wave
            fits_family = (
                current_family + ha_in_group <= effective_family_cap if enforce_family else True
            )
            if fits_vms and fits_family:
                return candidate
            candidate += 1
        # Should never reach here — fresh waves always satisfy fits_vms
        # since groups are split below max_vms_per_wave before this
        # method runs. Raise so the bug surfaces loudly rather than
        # silently producing a wedged plan.
        raise RuntimeError(
            f"Wave skeleton could not place group {group.id!r} within "
            f"{max_waves} waves — likely a logic bug, not a data issue."
        )

    @staticmethod
    def _finalize(waves: dict[int, Wave]) -> list[Wave]:
        # Renumber 1..N so non-contiguous wave assignments (from a
        # dependency that pushed a group to wave 7 when 5 and 6 are
        # empty) collapse to a tight sequence. Operators always see
        # 1..N waves, never gaps.
        ordered = sorted(waves.values(), key=lambda w: w.wave_number)
        out: list[Wave] = []
        for idx, wave in enumerate(ordered, start=1):
            wave.wave_number = idx
            wave.groups.sort(
                key=lambda g: (
                    _ROLE_PRIORITY.get(g.estimated_role, 99),
                    _RISK_PRIORITY.get(g.migration_risk, 1),
                    g.id,
                ),
            )
            wave.estimated_risk = _wave_aggregate_risk(wave.groups)
            out.append(wave)
        return out


def _wave_aggregate_risk(groups: list[VMGroup]) -> str:
    """Wave's headline risk = max across its members."""
    if not groups:
        return "low"
    highest = max(_RISK_PRIORITY.get(g.migration_risk, 1) for g in groups)
    return {0: "low", 1: "medium", 2: "high"}[highest]
