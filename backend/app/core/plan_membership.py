"""Which plan holds which VM, and which partition a plan covers.

Neither fact is stored. Plan membership lives only in
``MigrationPlan.vm_ids`` / ``waves[].vm_ids`` (there is no VM→plan FK),
and a plan row does not record its ``(source vCenter, target cluster,
target namespace)`` partition — ``waves[].group_ids`` encode the VM's
*override* fields, not the resolved target. Both are derived here, at
read time, from the same resolver the MTV emitter uses
(``target_resolution.resolve_vms_iter``), so the answer can never drift
from what YAML export would actually produce.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.target_resolution import resolve_vms_iter
from app.models.plan import MigrationPlan
from app.models.vcenter import VCenterSource
from app.models.vm import VM


@dataclass(frozen=True)
class Partition:
    """One MTV Plan CR's scope: a source provider, a destination
    provider and a target namespace."""

    vcenter_id: int | None
    cluster_id: int
    namespace: str
    vcenter_name: str | None = None
    cluster_name: str | None = None

    @property
    def key(self) -> tuple:
        return (self.vcenter_id, self.cluster_id, self.namespace)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_vcenter_id": self.vcenter_id,
            "source_vcenter_name": self.vcenter_name,
            "target_cluster_id": self.cluster_id,
            "target_cluster_name": self.cluster_name,
            "target_namespace": self.namespace,
        }

    def label(self) -> str:
        vc = self.vcenter_name or (f"vCenter {self.vcenter_id}" if self.vcenter_id else "?")
        cluster = self.cluster_name or f"cluster {self.cluster_id}"
        return f"{vc} → {cluster} / {self.namespace}"


def resolve_partitions(db: Session, vms: Iterable[VM]) -> dict[int, Partition | None]:
    """Partition per VM id; ``None`` when the VM does not resolve to a
    target cluster + namespace."""
    rows = list(vms)
    if not rows:
        return {}
    resolutions = resolve_vms_iter(rows, db)
    vc_ids = {vm.source_vcenter_id for vm in rows if vm.source_vcenter_id is not None}
    vc_names: dict[int, str] = {}
    if vc_ids:
        vc_names = {
            v.id: v.name
            for v in db.scalars(select(VCenterSource).where(VCenterSource.id.in_(vc_ids))).all()
        }
    out: dict[int, Partition | None] = {}
    for vm in rows:
        r = resolutions.get(vm.id)
        if r is None or r.cluster_id is None or not r.namespace:
            out[vm.id] = None
            continue
        out[vm.id] = Partition(
            vcenter_id=vm.source_vcenter_id,
            cluster_id=r.cluster_id,
            namespace=r.namespace,
            vcenter_name=vc_names.get(vm.source_vcenter_id),
            cluster_name=r.cluster_name,
        )
    return out


# A plan's VMs all share one partition (POST /api/plans partitions before
# it batches, and edits refuse cross-partition moves), so a single VM
# answers for the plan. A few candidates are tried in case the first was
# deleted from inventory after planning.
_REPRESENTATIVE_CANDIDATES = 3


def plan_partitions(db: Session, plans: Iterable[MigrationPlan]) -> dict[int, Partition | None]:
    """Partition per plan id, resolved from one representative VM each."""
    plan_rows = list(plans)
    candidates: dict[int, list[int]] = {
        p.id: [int(v) for v in (p.vm_ids or [])[:_REPRESENTATIVE_CANDIDATES]] for p in plan_rows
    }
    wanted = {vid for ids in candidates.values() for vid in ids}
    if not wanted:
        return {p.id: None for p in plan_rows}
    vm_by_id = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(wanted))).all()}
    representative: dict[int, VM] = {}
    for plan_id, ids in candidates.items():
        vm = next((vm_by_id[v] for v in ids if v in vm_by_id), None)
        if vm is not None:
            representative[plan_id] = vm
    by_vm = resolve_partitions(db, representative.values())
    return {
        p.id: (by_vm.get(representative[p.id].id) if p.id in representative else None)
        for p in plan_rows
    }


def wave_number_of(waves: list[dict] | None, vm_id: int) -> int | None:
    for w in waves or []:
        if isinstance(w, dict) and vm_id in (w.get("vm_ids") or []):
            return w.get("wave_number")
    return None


def vm_plan_index(
    db: Session, vm_ids: Iterable[int], *, with_wave: bool = False
) -> dict[int, dict[str, Any]]:
    """``{vm_id: {plan_id, plan_name, plan_wave_number}}`` for the VMs
    that are in a plan.

    Failed plans are skipped — they released their VMs. When a VM appears
    in several rows (it was migrated, rolled back and planned again) the
    newest plan wins. ``plan_wave_number`` is filled only when
    ``with_wave`` is set, which costs one extra read of the matched plans'
    wave documents.
    """
    wanted = set(vm_ids)
    if not wanted:
        return {}
    rows = db.execute(
        select(MigrationPlan.id, MigrationPlan.name, MigrationPlan.vm_ids)
        .where(MigrationPlan.status != "failed")
        .order_by(MigrationPlan.id.asc())
    ).all()
    out: dict[int, dict[str, Any]] = {}
    for plan_id, name, plan_vm_ids in rows:
        for vid in wanted.intersection(plan_vm_ids or []):
            out[vid] = {"plan_id": plan_id, "plan_name": name, "plan_wave_number": None}
    if with_wave and out:
        plan_ids = {entry["plan_id"] for entry in out.values()}
        waves_by_plan = dict(
            db.execute(
                select(MigrationPlan.id, MigrationPlan.waves).where(MigrationPlan.id.in_(plan_ids))
            ).all()
        )
        for vid, entry in out.items():
            entry["plan_wave_number"] = wave_number_of(waves_by_plan.get(entry["plan_id"]), vid)
    return out
