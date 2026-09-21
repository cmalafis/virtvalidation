"""Fleet-level migratability assessment.

Per-VM findings live on the VM (``GET /api/vms/{id}/assessment``) and are
filterable from the inventory listing. This router answers the estate
question — "how much of this can migrate, and what is in the way?" — and
lets an operator re-run the rules without re-importing.
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.assessment import RULESET_VERSION, assess, facts_from_vm
from app.core.audit import record_audit
from app.core.db import get_db
from app.models.vm import VM

router = APIRouter(tags=["assessment"])

_BATCH = 500


@router.get("/summary")
def assessment_summary(
    vcenter_source_id: list[int] | None = Query(default=None),
    environment: list[str] | None = Query(default=None),
    db: Session = Depends(get_db),
) -> dict:
    stmt = select(VM.assessment_status, VM.assessment)
    if vcenter_source_id:
        stmt = stmt.where(VM.source_vcenter_id.in_(vcenter_source_id))
    if environment:
        stmt = stmt.where(VM.environment.in_(environment))

    by_status: Counter = Counter()
    by_finding: dict[str, dict] = {}
    not_evaluated: dict[str, dict] = {}
    total = 0
    for status, doc in db.execute(stmt.execution_options(yield_per=_BATCH)):
        total += 1
        by_status[status] += 1
        for f in (doc or {}).get("findings") or []:
            row = by_finding.setdefault(
                f["id"],
                {
                    "id": f["id"],
                    "category": f["category"],
                    "label": f["label"],
                    "applies_to": f.get("applies_to", "all"),
                    "vm_count": 0,
                },
            )
            row["vm_count"] += 1
        for s in (doc or {}).get("not_evaluated") or []:
            row = not_evaluated.setdefault(
                s["id"], {"id": s["id"], "label": s["label"], "reason": s["reason"], "vm_count": 0}
            )
            row["vm_count"] += 1

    order = {"Critical": 0, "Warning": 1, "Information": 2}
    return {
        "ruleset": RULESET_VERSION,
        "total": total,
        "by_status": {k: by_status.get(k, 0) for k in ("blocked", "warning", "ok", "unknown")},
        "findings": sorted(
            by_finding.values(), key=lambda r: (order.get(r["category"], 9), -r["vm_count"])
        ),
        # Rules that could not run, and on how many VMs. A finding count of
        # zero means nothing unless the rule actually ran.
        "not_evaluated": sorted(not_evaluated.values(), key=lambda r: -r["vm_count"]),
    }


@router.post("/run")
def run_assessment(request: Request, db: Session = Depends(get_db)) -> dict:
    """Re-assess every VM against the current rule set. Cheap and
    deterministic (pure Python over stored facts); safe to repeat."""
    changed = 0
    total = 0
    last_id = 0
    while True:
        rows = list(
            db.scalars(select(VM).where(VM.id > last_id).order_by(VM.id).limit(_BATCH)).all()
        )
        if not rows:
            break
        for vm in rows:
            total += 1
            result = assess(facts_from_vm(vm)).to_dict()
            if vm.assessment != result:
                vm.assessment = result
                vm.assessment_status = result["status"]
                vm.assessment_finding_ids = [f["id"] for f in result["findings"]]
                changed += 1
        last_id = rows[-1].id
        db.commit()
    record_audit(
        db,
        action="assessment.run",
        actor=request.headers.get("x-actor", "user"),
        resource_type="assessment",
        resource_id=None,
        details={"ruleset": RULESET_VERSION, "assessed": total, "changed": changed},
    )
    db.commit()
    return {"ruleset": RULESET_VERSION, "assessed": total, "changed": changed}
