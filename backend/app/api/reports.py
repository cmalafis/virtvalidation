"""Dashboard-level report endpoints.

Five reports the Reports tab links to. Contract for all of them:

  - **Always 200.** Empty data is a valid state, not an error. The
    frontend prefers an empty-state message over a 404.
  - **Read-only.** No side-effects, no audit rows.
  - **Self-describing.** Every payload carries a ``report_type`` string
    so the frontend can dispatch on a single field.

The executive summary endpoint optionally calls Ollama for an LLM-
generated narrative paragraph; if Ollama is unreachable it falls back
to a deterministic summary built from the same data so the endpoint
still returns 200 with useful content.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.db import get_db
from app.models.plan import MigrationPlan
from app.models.validation import ValidationResult
from app.models.vm import VM, BaselineSnapshot, VMStatus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------
# Sort order for "severity-first" lists. Failed VMs go before degraded,
# degraded before healthy, pending last — what an operator looking at a
# wave report wants to see at the top.
_VM_SORT_ORDER = {"failed": 0, "degraded": 1, "healthy": 2, "pending": 3}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _verdict_label(v: ValidationResult | None) -> str:
    """Map a ValidationResult.status (or its absence) to the dashboard's
    verdict vocabulary used by the UI: healthy / degraded / failed / pending."""
    if v is None:
        return "pending"
    return {
        "pass": "healthy",
        "warn": "degraded",
        "fail": "failed",
    }.get(v.status.value, "pending")


def _latest_validation_per_vm(db: Session) -> dict[int, ValidationResult]:
    rows = db.scalars(select(ValidationResult).order_by(ValidationResult.validated_at.asc())).all()
    out: dict[int, ValidationResult] = {}
    for row in rows:
        out[row.vm_id] = row  # later iterations overwrite — final = latest
    return out


def _latest_snapshot_per_vm(db: Session) -> dict[int, BaselineSnapshot]:
    rows = db.scalars(select(BaselineSnapshot).order_by(BaselineSnapshot.collected_at.asc())).all()
    out: dict[int, BaselineSnapshot] = {}
    for row in rows:
        out[row.vm_id] = row
    return out


def _vm_brief(vm: VM, validation: ValidationResult | None, status_label: str) -> dict:
    return {
        "id": vm.id,
        "name": vm.name,
        "hostname": vm.source_hostname,
        "ip_address": vm.ip_address,
        "os_family": vm.os_family,
        "role": vm.role,
        "environment": vm.environment,
        "owner": vm.owner,
        "status": status_label,
        "summary": validation.summary if validation else "",
        "findings": (validation.findings or []) if validation else [],
        "remediation": (validation.remediation or []) if validation else [],
        "validated_at": validation.validated_at.isoformat()
        if validation and validation.validated_at
        else None,
    }


def _sort_vms_by_severity(vms: list[dict]) -> list[dict]:
    """Failed → degraded → healthy → pending; ties broken by name."""
    return sorted(
        vms,
        key=lambda v: (_VM_SORT_ORDER.get(v["status"], 99), v["name"].lower()),
    )


def _aggregate_finding_severity(validations: list[ValidationResult]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for v in validations:
        for f in v.findings or []:
            sev = (f.get("severity") or "info").lower()
            counts[sev] += 1
    return {k: counts.get(k, 0) for k in ("critical", "warn", "info")}


# ---------------------------------------------------------------------------
# 1. /full-validation — every VM with its latest verdict, severity-sorted
# ---------------------------------------------------------------------------
@router.get("/full-validation")
def full_validation(db: Session = Depends(get_db)) -> dict:
    vms = list(db.scalars(select(VM)).all())
    validations = _latest_validation_per_vm(db)

    rows = []
    counts = {"healthy": 0, "degraded": 0, "failed": 0, "pending": 0}
    for vm in vms:
        v = validations.get(vm.id)
        label = _verdict_label(v)
        counts[label] = counts.get(label, 0) + 1
        rows.append(_vm_brief(vm, v, label))

    return {
        "report_type": "full_validation",
        "generated_at": _now_iso(),
        "summary": {
            "total": len(vms),
            **counts,
        },
        "vms": _sort_vms_by_severity(rows),
    }


# ---------------------------------------------------------------------------
# 2. /executive-summary — leadership-grade narrative + key risks
# ---------------------------------------------------------------------------
@router.get("/executive-summary")
def executive_summary(db: Session = Depends(get_db)) -> dict:
    vms = list(db.scalars(select(VM)).all())
    validations = _latest_validation_per_vm(db)

    counts = {"healthy": 0, "degraded": 0, "failed": 0, "pending": 0}
    for vm in vms:
        counts[_verdict_label(validations.get(vm.id))] += 1
    total = len(vms)
    progress_pct = (
        round(((counts["healthy"] + counts["degraded"] + counts["failed"]) / total) * 100)
        if total > 0
        else 0
    )
    finding_severity = _aggregate_finding_severity(list(validations.values()))

    plans = list(db.scalars(select(MigrationPlan).order_by(MigrationPlan.created_at.desc())).all())
    latest_plan = plans[0] if plans else None

    wave_status = _build_wave_status(latest_plan, validations) if latest_plan else []

    summary_block = {
        "total_vms": total,
        "by_status": counts,
        "migration_progress_pct": progress_pct,
        "findings_by_severity": finding_severity,
        "plan_count": len(plans),
        "latest_plan_id": latest_plan.id if latest_plan else None,
    }

    key_risks = _top_risks(vms, validations, limit=5)
    narrative = _llm_executive_summary(summary_block, wave_status, key_risks)

    return {
        "report_type": "executive_summary",
        "generated_at": _now_iso(),
        "summary": summary_block,
        "executive_summary": narrative,
        "wave_status": wave_status,
        "key_risks": key_risks,
    }


def _build_wave_status(plan: MigrationPlan, validations: dict[int, ValidationResult]) -> list[dict]:
    """For each wave, count how many of its VMs have already been
    validated and what the verdict mix looks like. Drives the executive
    report's "wave completion" column."""
    waves = []
    for wave in plan.waves or []:
        vm_ids = list(wave.get("vm_ids") or [])
        wave_counts = {"healthy": 0, "degraded": 0, "failed": 0, "pending": 0}
        for vid in vm_ids:
            wave_counts[_verdict_label(validations.get(vid))] += 1
        validated = wave_counts["healthy"] + wave_counts["degraded"] + wave_counts["failed"]
        waves.append(
            {
                "wave_number": wave.get("wave_number"),
                "vm_count": len(vm_ids),
                "validated_count": validated,
                "completion_pct": round((validated / len(vm_ids)) * 100) if vm_ids else 0,
                "estimated_risk": wave.get("estimated_risk"),
                "rationale": wave.get("rationale", ""),
                **wave_counts,
            }
        )
    return waves


def _top_risks(
    vms: list[VM],
    validations: dict[int, ValidationResult],
    *,
    limit: int = 5,
) -> list[dict]:
    """Top risks across the migration. A "risk" is either:

      - a VM with a failed verdict (highest priority)
      - a VM with a degraded verdict that has a critical-severity finding
      - a VM with no baseline yet but an enrolled status (capture gap)

    Sorted by severity then by VM name for determinism.
    """
    risks: list[dict] = []
    for vm in vms:
        v = validations.get(vm.id)
        label = _verdict_label(v)
        if label == "failed" and v is not None:
            risks.append(
                {
                    "vm_id": vm.id,
                    "vm_name": vm.name,
                    "severity": "critical",
                    "category": "validation_failure",
                    "description": v.summary or "Post-migration validation failed.",
                }
            )
        elif label == "degraded" and v is not None:
            crit = next(
                (f for f in (v.findings or []) if (f.get("severity") or "").lower() == "critical"),
                None,
            )
            if crit:
                risks.append(
                    {
                        "vm_id": vm.id,
                        "vm_name": vm.name,
                        "severity": "high",
                        "category": "critical_finding_on_degraded_vm",
                        "description": crit.get(
                            "message", v.summary or "Degraded with critical finding"
                        ),
                    }
                )
        elif vm.status == VMStatus.discovered:
            risks.append(
                {
                    "vm_id": vm.id,
                    "vm_name": vm.name,
                    "severity": "medium",
                    "category": "no_baseline",
                    "description": "Enrolled but no baseline snapshot has been captured yet.",
                }
            )

    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    risks.sort(key=lambda r: (severity_rank.get(r["severity"], 9), r["vm_name"].lower()))
    return risks[:limit]


def _llm_executive_summary(
    summary: dict,
    wave_status: list[dict],
    key_risks: list[dict],
) -> str:
    """Produce a narrative paragraph from the aggregate stats.

    Tries Ollama first; falls back to a deterministic summary if Ollama
    is unreachable or slow. The fallback path is what keeps this endpoint
    a true "always 200" — leadership reports shouldn't 503 because the
    LLM container is restarting.
    """
    fallback = _deterministic_summary(summary, wave_status, key_risks)

    system_prompt = (
        "You are VirtValidate's executive summarizer. Write ONE paragraph "
        "(3-5 sentences) suitable for a CIO or migration steering committee. "
        "Cover: overall migration progress, the worst current risk in plain "
        "English, and a recommendation. No bullet points; no JSON; no "
        "preamble. Just the paragraph."
    )
    user_prompt = (
        f"Summary stats: {summary}\n\n"
        f"Wave status: {wave_status}\n\n"
        f"Key risks: {key_risks}\n\n"
        f"Deterministic baseline (rewrite this in your own words, "
        f"keeping all numbers exact):\n{fallback}"
    )

    payload = {
        "model": app_settings.ollama_model,
        "stream": False,
        "options": {"temperature": 0.2},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    try:
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(f"{app_settings.ollama_host.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
            body = resp.json()
        content = (body.get("message") or {}).get("content", "").strip()
        if content:
            return content
        logger.warning("executive_summary: Ollama returned empty content; using fallback")
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("executive_summary: Ollama unreachable (%s); using fallback", e)

    return fallback


def _deterministic_summary(
    summary: dict,
    wave_status: list[dict],
    key_risks: list[dict],
) -> str:
    """Last-resort narrative built without an LLM. Same numbers, plainer prose."""
    total = summary.get("total_vms", 0)
    if total == 0:
        return (
            "No VMs are enrolled yet. Once the team adds VMs and the "
            "appliance captures baselines, this report will summarize "
            "validation progress and surface migration risks."
        )

    counts = summary.get("by_status", {})
    pct = summary.get("migration_progress_pct", 0)
    healthy = counts.get("healthy", 0)
    degraded = counts.get("degraded", 0)
    failed = counts.get("failed", 0)
    pending = counts.get("pending", 0)

    parts = [
        f"{total} VMs are enrolled. Validation progress is {pct}%, with "
        f"{healthy} healthy, {degraded} degraded, {failed} failed, and "
        f"{pending} pending validation."
    ]
    if wave_status:
        complete_waves = sum(1 for w in wave_status if w["completion_pct"] >= 100)
        parts.append(
            f"The latest migration plan has {len(wave_status)} waves; "
            f"{complete_waves} are fully validated."
        )
    if key_risks:
        top = key_risks[0]
        parts.append(f"Top risk: {top['vm_name']} ({top['severity']}) — {top['description']}")
    if failed > 0:
        parts.append("Recommend remediating failed VMs before proceeding to the next wave.")
    elif degraded > 0:
        parts.append(
            "Recommend reviewing degraded VMs' findings before declaring the migration complete."
        )
    else:
        parts.append("No urgent action required; continue the scheduled migration cadence.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# 3. /failed-degraded — same shape as full-validation, filtered
# ---------------------------------------------------------------------------
@router.get("/failed-degraded")
def failed_degraded(db: Session = Depends(get_db)) -> dict:
    vms = list(db.scalars(select(VM)).all())
    validations = _latest_validation_per_vm(db)

    rows = []
    counts = {"degraded": 0, "failed": 0}
    for vm in vms:
        v = validations.get(vm.id)
        label = _verdict_label(v)
        if label not in {"degraded", "failed"}:
            continue
        counts[label] = counts.get(label, 0) + 1
        rows.append(_vm_brief(vm, v, label))

    return {
        "report_type": "failed_degraded",
        "generated_at": _now_iso(),
        "summary": {
            "total_in_inventory": len(vms),
            "needing_action": len(rows),
            **counts,
        },
        "vms": _sort_vms_by_severity(rows),
    }


# ---------------------------------------------------------------------------
# 4. /wave-plan — most recent plan with waves + VM details
# ---------------------------------------------------------------------------
@router.get("/wave-plan")
def wave_plan(db: Session = Depends(get_db)) -> dict:
    plan = db.scalars(
        select(MigrationPlan).order_by(MigrationPlan.created_at.desc()).limit(1)
    ).first()

    if plan is None:
        # Empty-but-not-error: the frontend renders the no_plan_message
        # rather than an error state.
        return {
            "report_type": "wave_plan",
            "generated_at": _now_iso(),
            "plan_id": None,
            "plan_created_at": None,
            "plan_summary": "",
            "model": "",
            "waves": [],
            "no_plan_message": "Generate a migration plan first",
        }

    validations = _latest_validation_per_vm(db)
    vm_ids: set[int] = set()
    for w in plan.waves or []:
        vm_ids.update(w.get("vm_ids") or [])
    vms = {vm.id: vm for vm in db.scalars(select(VM).where(VM.id.in_(vm_ids))).all()}

    waves = []
    for w in plan.waves or []:
        members = []
        wave_counts = {"healthy": 0, "degraded": 0, "failed": 0, "pending": 0}
        for vid in w.get("vm_ids") or []:
            vm = vms.get(vid)
            v = validations.get(vid)
            label = _verdict_label(v)
            wave_counts[label] += 1
            if vm is None:
                # Plan references a VM that's been deleted — surface it
                # rather than silently dropping the row.
                members.append(
                    {
                        "vm_id": vid,
                        "name": f"(deleted vm_id={vid})",
                        "status": "unknown",
                        "ip_address": None,
                    }
                )
                continue
            members.append(
                {
                    "vm_id": vm.id,
                    "name": vm.name,
                    "ip_address": vm.ip_address,
                    "os_family": vm.os_family,
                    "role": vm.role,
                    "status": label,
                }
            )
        validated = wave_counts["healthy"] + wave_counts["degraded"] + wave_counts["failed"]
        wave_status_label = (
            "complete"
            if wave_counts.get("pending", 0) == 0 and members
            else "in_progress"
            if validated > 0
            else "pending"
        )
        waves.append(
            {
                "wave_number": w.get("wave_number"),
                "rationale": w.get("rationale", ""),
                "estimated_risk": w.get("estimated_risk"),
                "vm_count": len(members),
                "status": wave_status_label,
                "verdicts": wave_counts,
                "vms": members,
            }
        )

    return {
        "report_type": "wave_plan",
        "generated_at": _now_iso(),
        "plan_id": plan.id,
        "plan_created_at": plan.created_at.isoformat(),
        "plan_summary": plan.summary or "",
        "model": plan.model,
        "waves": waves,
        "no_plan_message": None,
    }


# ---------------------------------------------------------------------------
# 5. /baseline-snapshot — current baseline state per VM
# ---------------------------------------------------------------------------
@router.get("/baseline-snapshot")
def baseline_snapshot(db: Session = Depends(get_db)) -> dict:
    vms = list(db.scalars(select(VM).order_by(VM.name)).all())
    snapshots = _latest_snapshot_per_vm(db)
    snapshot_count_by_vm: dict[int, int] = {
        vm.id: (
            db.scalar(
                select(func.count(BaselineSnapshot.id)).where(BaselineSnapshot.vm_id == vm.id)
            )
            or 0
        )
        for vm in vms
    }

    latest_collected_at: datetime | None = None
    rows = []
    for vm in vms:
        snap = snapshots.get(vm.id)
        meta = (snap.raw_data or {}).get("meta", {}) if snap else {}
        services = (snap.raw_data or {}).get("services", []) if snap else []
        ports = (snap.raw_data or {}).get("ports", []) if snap else []
        mounts = (snap.raw_data or {}).get("mounts", []) if snap else []
        if snap and snap.collected_at:
            if latest_collected_at is None or snap.collected_at > latest_collected_at:
                latest_collected_at = snap.collected_at
        rows.append(
            {
                "vm_id": vm.id,
                "name": vm.name,
                "hostname": vm.source_hostname,
                "ip_address": vm.ip_address,
                "os_family": vm.os_family,
                "os_profile": meta.get("os_profile"),
                "service_count": len(services),
                "port_count": len(ports),
                "mount_count": len(mounts),
                "snapshot_count": snapshot_count_by_vm.get(vm.id, 0),
                "last_collected_at": snap.collected_at.isoformat()
                if snap and snap.collected_at
                else None,
            }
        )

    vms_with_baselines = sum(1 for r in rows if r["snapshot_count"] > 0)

    return {
        "report_type": "baseline_snapshot",
        "generated_at": _now_iso(),
        "summary": {
            "total_vms": len(vms),
            "vms_with_baselines": vms_with_baselines,
            "vms_without_baselines": len(vms) - vms_with_baselines,
            "latest_collection_at": latest_collected_at.isoformat()
            if latest_collected_at
            else None,
        },
        "vms": rows,
    }
