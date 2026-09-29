"""Split one (vCenter, cluster, namespace) partition into plan-sized batches.

``POST /api/plans`` has no selection cap. A selection larger than
``settings.max_vms_per_plan`` is generated as a *series* of plans, each
holding at most that many VMs, run one after another by the existing
BackgroundTasks path. This module owns the two pure-Python decisions
that make a series reproducible:

  * **Which VMs land in which batch.** VMs are sorted so that the ones
    the pre-classifier would group together (same ``application_hint``,
    same HA family) are adjacent, then cut into balanced slices. A
    family may still straddle a boundary; that is harmless because two
    plans never share a wave, so the anti-affinity rule (≤2 peers per
    wave) is trivially satisfied across the cut.
  * **What each plan is called.** ``base-1``, ``base-2`` … series-wide,
    with the partition slug kept in the middle when the selection also
    spans several partitions (``base-vc01-prod-1``). The number is
    appended *after* the RFC1123 truncation so it can never be cut off.

No LLM involvement — this is mechanical orchestration per the
"LLM Input Discipline" rule in CLAUDE.md.
"""

from __future__ import annotations

from math import ceil
from typing import TYPE_CHECKING

from app.core.family import detect_family

if TYPE_CHECKING:  # pragma: no cover
    from app.models.vm import VM

#: Kubernetes object names (and therefore the MTV bundle names derived
#: from plan names) are capped at 63 characters.
PLAN_NAME_SLUG_MAX_LEN = 63


def _chunk_sort_key(vm: VM) -> tuple[str, str, str, int]:
    return (
        (getattr(vm, "application_hint", None) or "").lower(),
        detect_family(vm.name or ""),
        (vm.name or "").lower(),
        int(vm.id or 0),
    )


def chunk_partition(vms: list[VM], max_per_plan: int) -> list[list[VM]]:
    """Cut ``vms`` into ``ceil(len / max_per_plan)`` balanced batches.

    Returns ``[vms]`` untouched (original order preserved) when the
    partition already fits in one plan, so the single-plan path looks
    exactly as it did before batching existed.

    Batches are balanced — sizes differ by at most one — so a 3007-VM
    selection becomes four batches of 752/752/752/751 rather than
    1000/1000/1000/7. ``max_per_plan`` is a hard ceiling on every batch.
    The output is deterministic for a given set of VMs regardless of
    input order.
    """
    if max_per_plan <= 0:
        raise ValueError("max_per_plan must be positive")
    if len(vms) <= max_per_plan:
        return [list(vms)]

    ordered = sorted(vms, key=_chunk_sort_key)
    n_chunks = ceil(len(ordered) / max_per_plan)
    base, extra = divmod(len(ordered), n_chunks)

    chunks: list[list[VM]] = []
    start = 0
    for i in range(n_chunks):
        size = base + (1 if i < extra else 0)
        chunks.append(ordered[start : start + size])
        start += size
    return chunks


def slug_for_plan_name(value: str, max_len: int = PLAN_NAME_SLUG_MAX_LEN) -> str:
    """RFC1123-safe lowercase slug. Falls back to ``plan`` when empty."""
    out: list[str] = []
    last_dash = False
    for ch in value.lower():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash and out:
            out.append("-")
            last_dash = True
    slug = "".join(out).strip("-")
    return (slug or "plan")[:max_len].rstrip("-") or "plan"


def compose_series_name(
    base: str,
    *,
    vcenter_name: str | None = None,
    cluster_name: str | None = None,
    namespace: str | None = None,
    index: int = 1,
    total: int = 1,
    multi_partition: bool = False,
) -> str:
    """Name one plan of a series.

    * ``total == 1`` and a single partition → ``base`` verbatim, so the
      simple case is unchanged from pre-batching behaviour.
    * Several partitions → partition parts are slugged into the name
      (``base-vc-cluster-ns``), as the fan-out has always done.
    * ``total > 1`` → ``-{index}`` is appended after truncation.
    """
    if total <= 1 and not multi_partition:
        return base

    parts = [base]
    if multi_partition:
        for s in (vcenter_name, cluster_name, namespace):
            if s:
                parts.append(s)
    stem = "-".join(parts)

    if total <= 1:
        return slug_for_plan_name(stem)

    suffix = f"-{index}"
    return slug_for_plan_name(stem, max_len=PLAN_NAME_SLUG_MAX_LEN - len(suffix)) + suffix


__all__ = [
    "PLAN_NAME_SLUG_MAX_LEN",
    "chunk_partition",
    "compose_series_name",
    "slug_for_plan_name",
]
