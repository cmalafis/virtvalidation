"""Planner-rule checks for a hand-edited wave layout — pure Python.

``MechanicalWaveAssigner`` enforces its limits while it packs (the checks
live inside ``_find_target_wave`` and depend on packer state), so there is
nothing to call when an operator rearranges a finished plan. This module
is that check: given wave membership as plain ``{wave_number: [(vm_id,
name)]}`` it reports which waves break a rule, as structured
:class:`Violation` rows the API returns verbatim.

Two rules, both ``overridable`` — the operator may know a dependency the
estate data does not show:

  - **wave_size** — more than ``MAX_VMS_PER_WAVE`` VMs in one wave.
  - **ha_family_cap** — more members of one name family in a wave than
    ``family_cap(plan-wide family size)`` allows.

The family notion is ``family.detect_family`` on the VM name, the same key
Stage 3 splits on and Stage 5 colours by. The packer's own HA key
(``wave_skeleton._ha_family_key``) reads preclassifier group metadata,
which a hand-built wave does not have a stable version of; its "a primary
never shares a wave with a peer" lock-out is therefore NOT reproduced here.

Generated plans can already exceed the name-family cap (the packer may
re-combine groups Stage 3 split), so callers report
:func:`diff_violations` — what an edit introduces or worsens — rather
than the standing set.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.core.family import detect_family, family_cap
from app.core.wave_skeleton import MAX_VMS_PER_WAVE

#: ``{wave_number: [(vm_id, vm_name), ...]}``
WaveMembers = dict[int, list[tuple[int, str]]]

BLOCKING = "blocking"
OVERRIDABLE = "overridable"
INFO = "info"


@dataclass(frozen=True)
class Violation:
    """One rule a wave layout breaks (or a fact the operator should see).

    ``severity`` drives the API contract: ``blocking`` → the edit is
    refused, ``overridable`` → needs ``confirm_overrides``, ``info`` →
    shown, never gates.
    """

    code: str
    severity: str
    message: str
    plan_id: int | None = None
    wave_number: int | None = None
    family: str | None = None
    vm_ids: tuple[int, ...] = ()
    vm_names: tuple[str, ...] = ()
    count: int | None = None
    limit: int | None = None

    @property
    def key(self) -> tuple:
        return (self.code, self.plan_id, self.wave_number, self.family)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "plan_id": self.plan_id,
            "wave_number": self.wave_number,
            "family": self.family,
            "vm_ids": list(self.vm_ids),
            "vm_names": list(self.vm_names),
            "count": self.count,
            "limit": self.limit,
        }


def wave_size_violations(
    waves: WaveMembers,
    *,
    plan_id: int | None = None,
    max_vms: int = MAX_VMS_PER_WAVE,
) -> list[Violation]:
    out: list[Violation] = []
    for number in sorted(waves):
        members = waves[number]
        if len(members) <= max_vms:
            continue
        out.append(
            Violation(
                code="wave_size",
                severity=OVERRIDABLE,
                message=(
                    f"Wave {number} would hold {len(members)} VMs; the planner keeps waves "
                    f"to {max_vms} because MTV throttles concurrent migrations per ESXi host."
                ),
                plan_id=plan_id,
                wave_number=number,
                count=len(members),
                limit=max_vms,
            )
        )
    return out


def family_sizes(waves: WaveMembers) -> Counter[str]:
    """Plan-wide member count per name family."""
    sizes: Counter[str] = Counter()
    for members in waves.values():
        for _vid, name in members:
            sizes[detect_family(name)] += 1
    return sizes


def ha_family_violations(waves: WaveMembers, *, plan_id: int | None = None) -> list[Violation]:
    """Waves holding more of one family than ``family_cap`` allows.

    A family of one never violates. The cap is computed from the family's
    size across the whole layout, so removing a member elsewhere can
    tighten the cap on a wave the edit did not touch — callers check
    every wave of the plan, before and after.
    """
    sizes = family_sizes(waves)
    out: list[Violation] = []
    for number in sorted(waves):
        by_family: dict[str, list[tuple[int, str]]] = {}
        for vid, name in waves[number]:
            by_family.setdefault(detect_family(name), []).append((vid, name))
        for family in sorted(by_family):
            size = sizes[family]
            if size < 2:
                continue
            cap = family_cap(size)
            present = by_family[family]
            if len(present) <= cap:
                continue
            out.append(
                Violation(
                    code="ha_family_cap",
                    severity=OVERRIDABLE,
                    message=(
                        f"Wave {number} would hold {len(present)} of the {size} "
                        f"'{family}' VMs ({', '.join(n for _, n in present[:6])}"
                        f"{'…' if len(present) > 6 else ''}); the planner allows at most "
                        f"{cap} per wave so the source cluster keeps quorum."
                    ),
                    plan_id=plan_id,
                    wave_number=number,
                    family=family,
                    vm_ids=tuple(v for v, _ in present),
                    vm_names=tuple(n for _, n in present),
                    count=len(present),
                    limit=cap,
                )
            )
    return out


def check_waves(waves: WaveMembers, *, plan_id: int | None = None) -> list[Violation]:
    """Every standing rule violation in this layout."""
    return wave_size_violations(waves, plan_id=plan_id) + ha_family_violations(
        waves, plan_id=plan_id
    )


def diff_violations(before: list[Violation], after: list[Violation]) -> list[Violation]:
    """Violations in ``after`` that an edit introduced or made worse.

    Matched on ``(code, plan, wave, family)``; a violation that already
    stood with the same or a higher count is not the edit's doing.
    """
    prior = {v.key: (v.count or 0) for v in before}
    return [v for v in after if v.key not in prior or (v.count or 0) > prior[v.key]]
