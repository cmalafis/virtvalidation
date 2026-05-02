"""Network design review HTTP surface.

CRUD + analyze endpoints. Analysis runs as a FastAPI BackgroundTask;
status lives on the review row itself (status: draft → analyzing →
completed/failed) so polling survives appliance restarts and we don't
need a separate in-memory task store like the capture flow.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core import db as _db_module  # late-binding for test rebind compat
from app.core.audit import record_audit
from app.core.db import get_db
from app.core.network_review import (
    NetworkReviewer,
    NetworkReviewError,
    build_source_summary,
)
from app.models.network_review import (
    FindingCategory,
    FindingConfidence,
    FindingSeverity,
    FindingTriage,
    NetworkDesignReview,
    NetworkFinding,
    NetworkReviewStatus,
)
from app.schemas.network_review import (
    NetworkFindingRead,
    NetworkFindingTriageUpdate,
    NetworkReviewCreate,
    NetworkReviewNotesUpdate,
    NetworkReviewRead,
    NetworkReviewSummary,
    NetworkReviewYamlUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["network-reviews"])


# ---------------------------------------------------------------------------
# Background task — runs the analyzer + persists results
# ---------------------------------------------------------------------------
def _run_analysis(review_id: int) -> None:
    """BackgroundTask body. Opens its own session, swaps the review's
    status flag through ``analyzing`` → ``completed`` / ``failed``, and
    persists per-finding rows once the LLM returns."""
    db = _db_module.SessionLocal()
    try:
        review = db.get(NetworkDesignReview, review_id)
        if review is None:
            logger.warning("network analysis: review %s vanished before task ran", review_id)
            return
        try:
            source_summary = build_source_summary(db)
            reviewer = NetworkReviewer()
            result = reviewer.analyze(
                source_summary=source_summary,
                customer_notes=review.customer_notes,
                proposed_yaml=review.proposed_yaml,
            )
        except NetworkReviewError as e:
            logger.warning("network analysis failed for review %s: %s", review_id, e)
            review.status = NetworkReviewStatus.failed
            review.last_error = str(e)
            db.commit()
            return
        except Exception as e:  # noqa: BLE001 — last-resort catchall in a background task
            logger.exception("network analysis crashed for review %s", review_id)
            review.status = NetworkReviewStatus.failed
            review.last_error = f"{type(e).__name__}: {e}"
            db.commit()
            return

        # Wipe previous findings; the analyzer is idempotent and re-runs
        # produce a fresh set. Keeping stale rows would mislead operators.
        for old in list(review.findings):
            db.delete(old)

        for f in result["findings"]:
            db.add(
                NetworkFinding(
                    review_id=review.id,
                    category=FindingCategory(f["category"]),
                    severity=FindingSeverity(f["severity"]),
                    confidence=FindingConfidence(f["confidence"]),
                    triage=FindingTriage.open,
                    title=f["title"],
                    description=f["description"],
                    source_evidence=f["source_evidence"],
                    proposed_evidence=f["proposed_evidence"],
                    recommendation=f["recommendation"],
                )
            )

        review.analysis_results = {
            "executive_summary": result["executive_summary"],
            "model": reviewer.backend.default_model,
            "backend": reviewer.backend.backend_type,
            "source_summary": source_summary,
        }
        review.status = NetworkReviewStatus.completed
        review.last_analyzed_at = datetime.now(timezone.utc)
        review.last_error = None
        db.commit()
        logger.info(
            "network analysis stored for review %s (%d findings)",
            review_id,
            len(result["findings"]),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_review_or_404(db: Session, review_id: int) -> NetworkDesignReview:
    review = db.get(NetworkDesignReview, review_id)
    if review is None:
        raise HTTPException(status_code=404, detail=f"Network review {review_id} not found")
    return review


def _summary_for(review: NetworkDesignReview) -> dict:
    sev_counts: Counter[str] = Counter()
    for f in review.findings:
        sev_counts[f.severity.value] += 1
    return {
        "id": review.id,
        "name": review.name,
        "status": review.status,
        "finding_count": len(review.findings),
        "severity_counts": dict(sev_counts),
        "created_at": review.created_at,
        "updated_at": review.updated_at,
        "last_analyzed_at": review.last_analyzed_at,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@router.post("", response_model=NetworkReviewRead, status_code=status.HTTP_201_CREATED)
def create_review(
    request: Request,
    payload: NetworkReviewCreate,
    db: Session = Depends(get_db),
) -> NetworkDesignReview:
    review = NetworkDesignReview(
        name=payload.name,
        customer_notes=payload.customer_notes or "",
        proposed_yaml=payload.proposed_yaml or "",
        status=NetworkReviewStatus.draft,
        analysis_results={},
    )
    db.add(review)
    db.commit()
    db.refresh(review)

    record_audit(
        db,
        action="network_review.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="network_review",
        resource_id=review.id,
        details={"name": review.name},
    )
    db.commit()
    request.state.skip_audit_log = True
    return review


@router.get("", response_model=list[NetworkReviewSummary])
def list_reviews(db: Session = Depends(get_db)) -> list[dict]:
    """Index. Sorted newest first; embeds the finding+severity counts so
    the dashboard tab can render badges without a follow-up fetch."""
    reviews = list(
        db.scalars(
            select(NetworkDesignReview)
            .options(selectinload(NetworkDesignReview.findings))
            .order_by(NetworkDesignReview.created_at.desc())
        ).all()
    )
    return [_summary_for(r) for r in reviews]


@router.get("/{review_id}", response_model=NetworkReviewRead)
def get_review(review_id: int, db: Session = Depends(get_db)) -> NetworkDesignReview:
    return _get_review_or_404(db, review_id)


@router.put("/{review_id}/notes", response_model=NetworkReviewRead)
def update_notes(
    request: Request,
    review_id: int,
    payload: NetworkReviewNotesUpdate,
    db: Session = Depends(get_db),
) -> NetworkDesignReview:
    review = _get_review_or_404(db, review_id)
    review.customer_notes = payload.customer_notes
    db.commit()
    db.refresh(review)
    return review


@router.put("/{review_id}/yaml", response_model=NetworkReviewRead)
def update_yaml(
    request: Request,
    review_id: int,
    payload: NetworkReviewYamlUpdate,
    db: Session = Depends(get_db),
) -> NetworkDesignReview:
    review = _get_review_or_404(db, review_id)
    review.proposed_yaml = payload.proposed_yaml
    db.commit()
    db.refresh(review)
    return review


@router.post(
    "/{review_id}/analyze", response_model=NetworkReviewRead, status_code=status.HTTP_202_ACCEPTED
)
def analyze_review(
    request: Request,
    review_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> NetworkDesignReview:
    review = _get_review_or_404(db, review_id)
    if review.status == NetworkReviewStatus.analyzing:
        # Idempotent: re-issuing while already running is a no-op.
        return review
    review.status = NetworkReviewStatus.analyzing
    review.last_error = None
    db.commit()
    db.refresh(review)

    record_audit(
        db,
        action="network_review.analyze_triggered",
        actor=request.headers.get("x-actor", "user"),
        resource_type="network_review",
        resource_id=review.id,
        details={"name": review.name},
    )
    db.commit()
    request.state.skip_audit_log = True

    background_tasks.add_task(_run_analysis, review.id)
    return review


@router.delete("/{review_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_review(
    request: Request,
    review_id: int,
    db: Session = Depends(get_db),
) -> None:
    review = _get_review_or_404(db, review_id)
    record_audit(
        db,
        action="network_review.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="network_review",
        resource_id=review.id,
        details={"name": review.name},
    )
    db.delete(review)
    db.commit()
    request.state.skip_audit_log = True


@router.patch("/{review_id}/findings/{finding_id}", response_model=NetworkFindingRead)
def update_finding_triage(
    request: Request,
    review_id: int,
    finding_id: int,
    payload: NetworkFindingTriageUpdate,
    db: Session = Depends(get_db),
) -> NetworkFinding:
    finding = db.get(NetworkFinding, finding_id)
    if finding is None or finding.review_id != review_id:
        raise HTTPException(
            status_code=404,
            detail=f"Finding {finding_id} not found on review {review_id}",
        )
    finding.triage = FindingTriage(payload.triage)
    db.commit()
    db.refresh(finding)
    return finding
