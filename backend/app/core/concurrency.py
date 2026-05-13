"""Stage 5 — wave concurrency analysis (graph coloring).

Two waves are *concurrent-safe* iff:

  - they run against **different source vCenters** (different ESXi
    host pool — MTV Forklift can serialize them without source-side
    I/O contention), AND
  - they share **no family keys** in common (no two members of the
    same HA family migrate at the same time, even across waves).

We model this as a graph where nodes are waves and edges connect
waves that violate either rule. Greedy graph coloring in wave-number
order then assigns each wave a non-negative integer
``concurrency_group_id``: two waves share an id iff they are
parallel-safe.

The coloring is deterministic (wave-number iteration order +
lowest-available color). Federal audit reconstructs the same
concurrency layout months later from the same input plan.

Note: the rule is intentionally pessimistic — two waves in the same
vCenter ALWAYS get distinct ids even when no family overlap exists,
because MTV Forklift serializes per-source-vCenter migrations and
running two waves in parallel against one vCenter would only stress
the source side. Operators can override at apply time if a customer
deployment has a more permissive source-side topology.
"""

from __future__ import annotations

from app.core.family import detect_family
from app.core.wave_skeleton import Wave


def _family_keys_for_wave(wave: Wave, vm_name_by_id: dict[int, str]) -> set[str]:
    """Family keys represented by the VMs in this wave.

    A family key whose ``family_size_global`` is 1 contributes
    nothing (singletons aren't anti-affinity-relevant), but for the
    concurrency rule we still treat them as the wave's "fingerprint"
    so two waves migrating two members of a putative family across
    different vCenters don't collide. The cap pruning lives in
    family.split_overconcentrated_families; the concurrency check is
    deliberately stricter.
    """
    out: set[str] = set()
    for group in wave.groups:
        for vid in group.vm_ids:
            name = vm_name_by_id.get(vid)
            if not name:
                continue
            out.add(detect_family(name))
    return out


def _vcenters_for_wave(wave: Wave) -> set[int]:
    """Distinct source_vcenter_ids represented by the wave's groups."""
    return {g.key.vcenter_id for g in wave.groups if g.key.vcenter_id is not None}


def _conflicts(
    a: Wave,
    b: Wave,
    families_a: set[str],
    families_b: set[str],
) -> bool:
    """Two waves conflict if they share a vCenter or share family keys."""
    if _vcenters_for_wave(a) & _vcenters_for_wave(b):
        return True
    if families_a & families_b:
        return True
    return False


def assign_concurrency_groups(
    waves: list[Wave],
    vm_name_by_id: dict[int, str],
) -> list[Wave]:
    """Mutate ``waves`` in place to set ``concurrency_group_id`` on each.

    Greedy coloring in wave-number order. Returns the same list so
    callers can chain.
    """
    # Sort by wave_number to make coloring deterministic.
    ordered = sorted(waves, key=lambda w: w.wave_number)
    families_per_wave = {w.wave_number: _family_keys_for_wave(w, vm_name_by_id) for w in ordered}

    assignments: dict[int, int] = {}
    for w in ordered:
        # Find the smallest color id not in use by any conflicting
        # already-colored wave.
        used: set[int] = set()
        for other in ordered:
            if other.wave_number == w.wave_number:
                continue
            if other.wave_number not in assignments:
                continue
            if _conflicts(
                w,
                other,
                families_per_wave[w.wave_number],
                families_per_wave[other.wave_number],
            ):
                used.add(assignments[other.wave_number])
        color = 0
        while color in used:
            color += 1
        assignments[w.wave_number] = color
        w.concurrency_group_id = color
    return waves
