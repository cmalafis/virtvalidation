"""Name-based HA family detection.

Used by Stage 3 of the new pipeline to spread members of the same
"family" across waves so the original cluster keeps quorum during the
migration window.

A *family* is a set of VMs that share the same logical role at the
naming level — billing-db-014/036/043 are one family of three; AD-DC-01
and AD-DC-02 are one family of two; lonewolf-server-99 is its own
family of one (no anti-affinity treatment).

The function is intentionally simpler than the existing
``wave_skeleton._ha_family_key`` (which also reads VMGroup HA metadata):
``detect_family`` operates purely on the VM name. It's the floor —
every VM gets a family key; the anti-affinity rule decides whether
the family is large enough to split. The existing HA-aware logic in
the wave skeleton refines this with peer/replica role tagging when
that metadata exists, but families are guaranteed first.

Algorithm
---------

1. Tokenize on hyphens and underscores.
2. Strip trailing pure-digit tokens (``-01``, ``-037``, ``-043``).
3. If the last remaining token is a role suffix (``DC``, ``SQL``,
   ``DB``, …), record it and strip it.
4. Re-attach the role suffix to the stem so ``billing-app`` and
   ``billing-db`` resolve to different families even though they
   share the ``billing`` stem.
5. Lowercase + hyphen-join.

A VM whose name becomes empty after stripping (``"01"``,
``"-DC"``) returns the original name verbatim — no anti-affinity
treatment, but no crash.
"""

from __future__ import annotations

import re

#: Role suffixes commonly attached to VM names to identify the tier.
#: Order is intentionally irrelevant (set lookup); members are upper-
#: case so the comparison normalizes input casing in one place.
#: Extending this is safe — adding a suffix here narrows existing
#: families (e.g. adding ``CTRL`` would split ``billing-ctrl-01`` away
#: from ``billing-app-01``), which improves quorum protection but
#: never makes anti-affinity worse.
ROLE_SUFFIXES: frozenset[str] = frozenset(
    {
        "DC",
        "SQL",
        "DB",
        "WEB",
        "APP",
        "API",
        "CACHE",
        "MQ",
        "LB",
        "PROXY",
        "JUMP",
        "BASTION",
        "AUTH",
        "GW",
    }
)

_SEPARATORS = re.compile(r"[-_]")


def detect_family(vm_name: str) -> str:
    """Compute the family key for a VM name.

    Returns the lower-cased family key. Never returns ``None`` —
    a VM with no recognizable family pattern returns the lower-
    cased name verbatim, which means it lands in a family-of-one
    and the anti-affinity split leaves it alone.

    Examples::

        detect_family("AD-DC-01")        == "ad-dc"
        detect_family("AD-DC-02")        == "ad-dc"
        detect_family("billing-app-037") == "billing-app"
        detect_family("billing-db-014")  == "billing-db"
        detect_family("billing-db-036")  == "billing-db"
        detect_family("billing-db-043")  == "billing-db"
        detect_family("backup-s-app-13") == "backup-s-app"
        detect_family("lonewolf-99")     == "lonewolf"
        detect_family("noname")          == "noname"
    """
    if not vm_name:
        return ""
    parts = _SEPARATORS.split(vm_name)
    if not parts:
        return vm_name.lower()

    # 1. Strip trailing pure-digit tokens.
    while parts and parts[-1].isdigit():
        parts.pop()
    if not parts:
        # Nothing but digits — fall back to the original name so we
        # don't silently group unrelated digit-only VMs.
        return vm_name.lower()

    # 2. Strip + record a role suffix.
    role: str | None = None
    if parts[-1].upper() in ROLE_SUFFIXES:
        role = parts.pop().upper()
    if not parts:
        # The whole name was a role suffix (e.g. "DC"). Fall back to
        # the original name; no anti-affinity treatment.
        return vm_name.lower()

    base = "-".join(p.lower() for p in parts)
    return f"{base}-{role.lower()}" if role else base


def split_into_families(vm_names: list[str]) -> dict[str, list[str]]:
    """Convenience helper: group a list of VM names by family.

    Useful for tests + the Stage 3 splitter. The returned dict is
    ordered by first-seen family key for determinism.
    """
    families: dict[str, list[str]] = {}
    for name in vm_names:
        key = detect_family(name)
        families.setdefault(key, []).append(name)
    return families


def family_cap(family_size: int) -> int:
    """Maximum members of a family that may share one wave.

    The rule: no wave may contain more than ``ceil(family_size / 2)``
    members of a family with ``family_size >= 2``. A family-of-one
    receives no anti-affinity treatment (cap is 1, but that's also the
    family size — every singleton fits anywhere).

    Examples::

        family_cap(1)  == 1   # singleton — no constraint
        family_cap(2)  == 1   # never put both in one wave
        family_cap(3)  == 2   # at most 2 of 3 in one wave
        family_cap(4)  == 2
        family_cap(5)  == 3
        family_cap(11) == 6
    """
    if family_size <= 1:
        return 1
    return -(-family_size // 2)  # ceil(family_size / 2)


def split_overconcentrated_families(
    groups,
    vm_name_by_id: dict[int, str],
):
    """Stage 3: split any group that holds too many members of one family.

    For each group, compute family counts across its VM members. If
    any family has ``> family_cap(family_size_global)`` members in this
    one group, split the group into sub-groups so each sub-group's
    family count is within the cap. The wave skeleton then spreads
    those sub-groups across waves naturally (its existing per-wave
    family cap enforcement does the rest).

    The split is deterministic: members are partitioned by sorted
    ``vm_id``. Sub-groups inherit the parent's ``GroupKey`` with the
    discriminator suffixed ``/family-split-{n}``. All other fields
    (estimated_role, migration_risk, shared_attributes, ha_members)
    are inherited verbatim — the parent's classification was correct
    for the group's role; splitting is purely about spreading.

    Groups whose families are already within cap are returned
    untouched. The function is idempotent — running it twice produces
    the same result.

    The lazy import avoids a circular dep between ``family`` and
    ``preclassifier``; the preclassifier already imports from many
    other modules and we don't want to push it through the family
    detection path during model load.
    """
    from collections import Counter
    from dataclasses import replace

    from app.core.preclassifier import GroupKey, VMGroup

    # 1. Compute global family sizes across all groups (so we know the
    #    cap for each family).
    global_family_sizes: Counter[str] = Counter()
    for g in groups:
        for vid in g.vm_ids:
            name = vm_name_by_id.get(vid, "")
            if not name:
                continue
            global_family_sizes[detect_family(name)] += 1

    out: list[VMGroup] = []
    for g in groups:
        # Bucket this group's VMs by family.
        by_family: dict[str, list[int]] = {}
        for vid in g.vm_ids:
            name = vm_name_by_id.get(vid, "")
            fam = detect_family(name) if name else ""
            by_family.setdefault(fam, []).append(vid)

        # Decide whether any family exceeds the per-group cap.
        needs_split = False
        for fam, members in by_family.items():
            global_size = global_family_sizes.get(fam, 0)
            cap = family_cap(global_size)
            if len(members) > cap:
                needs_split = True
                break
        if not needs_split:
            out.append(g)
            continue

        # Allocate each VM to a sub-group bucket. We greedily distribute
        # members so no bucket exceeds the cap for any family.
        # Start with one bucket; grow as needed.
        buckets: list[list[int]] = [[]]
        bucket_family_counts: list[Counter[str]] = [Counter()]
        # Iterate in sorted-id order for determinism.
        for vid in sorted(g.vm_ids):
            fam = detect_family(vm_name_by_id.get(vid, ""))
            cap = family_cap(global_family_sizes.get(fam, 0))
            placed = False
            for idx, bucket in enumerate(buckets):
                if bucket_family_counts[idx][fam] < cap:
                    bucket.append(vid)
                    bucket_family_counts[idx][fam] += 1
                    placed = True
                    break
            if not placed:
                buckets.append([vid])
                new_counter: Counter[str] = Counter()
                new_counter[fam] = 1
                bucket_family_counts.append(new_counter)

        if len(buckets) == 1:
            out.append(g)
            continue

        for n, member_ids in enumerate(buckets, start=1):
            new_key = GroupKey(
                vcenter_id=g.key.vcenter_id,
                target_namespace=g.key.target_namespace,
                role=g.key.role,
                state=g.key.state,
                discriminator=f"{g.key.discriminator}/family-split-{n}",
                environment=g.key.environment,
                target_cluster_id=g.key.target_cluster_id,
            )
            ha_subset = [m for m in g.ha_members if m.vm_id in set(member_ids)]
            shared_attrs = {k: list(v) for k, v in g.shared_attributes.items()}
            note_suffix = f" [family-split {n}/{len(buckets)}: HA family cap enforced]"
            out.append(
                replace(
                    g,
                    key=new_key,
                    vm_ids=sorted(member_ids),
                    ha_members=ha_subset,
                    shared_attributes=shared_attrs,
                    notes=(g.notes or "") + note_suffix,
                )
            )

    return out
