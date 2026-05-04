"""Storage design review HTTP surface.

Sister router to ``app.api.network_reviews`` — same shape, storage
domain. CRUD + analyze endpoints; analysis runs as a FastAPI
BackgroundTask with status persisted on the review row itself.
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
from app.core.storage_review import (
    StorageReviewer,
    StorageReviewError,
    build_storage_source_summary,
)
from app.models.network_review import (
    FindingConfidence,
    FindingSeverity,
    FindingTriage,
)
from app.models.storage_review import (
    StorageDesignReview,
    StorageFinding,
    StorageFindingCategory,
    StorageReviewStatus,
)
from app.schemas.storage_review import (
    StorageFindingRead,
    StorageFindingTriageUpdate,
    StorageReviewCreate,
    StorageReviewNotesUpdate,
    StorageReviewRead,
    StorageReviewSummary,
    StorageReviewYamlUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["storage-reviews"])


# ---------------------------------------------------------------------------
# Background task — runs the analyzer + persists results
# ---------------------------------------------------------------------------
def _run_analysis(review_id: int) -> None:
    """BackgroundTask body. Mirrors the network-review pattern: opens
    its own session, swaps the review's status flag through
    ``analyzing`` → ``completed`` / ``failed``, and persists per-finding
    rows once the LLM returns."""
    db = _db_module.SessionLocal()
    try:
        review = db.get(StorageDesignReview, review_id)
        if review is None:
            logger.warning(
                "storage analysis: review %s vanished before task ran", review_id
            )
            return
        try:
            source_summary = build_storage_source_summary(db)
            reviewer = StorageReviewer()
            result = reviewer.analyze(
                source_summary=source_summary,
                customer_notes=review.customer_notes,
                proposed_yaml=review.proposed_yaml,
            )
        except StorageReviewError as e:
            logger.warning("storage analysis failed for review %s: %s", review_id, e)
            review.status = StorageReviewStatus.failed
            review.last_error = str(e)
            db.commit()
            return
        except Exception as e:  # noqa: BLE001 — last-resort catchall
            logger.exception("storage analysis crashed for review %s", review_id)
            review.status = StorageReviewStatus.failed
            review.last_error = f"{type(e).__name__}: {e}"
            db.commit()
            return

        # Wipe previous findings on re-run — analyzer is idempotent.
        for old in list(review.findings):
            db.delete(old)

        for f in result["findings"]:
            db.add(
                StorageFinding(
                    review_id=review.id,
                    category=StorageFindingCategory(f["category"]),
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
        review.status = StorageReviewStatus.completed
        review.last_analyzed_at = datetime.now(timezone.utc)
        review.last_error = None
        db.commit()
        logger.info(
            "storage analysis stored for review %s (%d findings)",
            review_id,
            len(result["findings"]),
        )
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _get_review_or_404(db: Session, review_id: int) -> StorageDesignReview:
    review = db.get(StorageDesignReview, review_id)
    if review is None:
        raise HTTPException(
            status_code=404, detail=f"Storage review {review_id} not found"
        )
    return review


def _summary_for(review: StorageDesignReview) -> dict:
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
@router.post("", response_model=StorageReviewRead, status_code=status.HTTP_201_CREATED)
def create_review(
    request: Request,
    payload: StorageReviewCreate,
    db: Session = Depends(get_db),
) -> StorageDesignReview:
    review = StorageDesignReview(
        name=payload.name,
        customer_notes=payload.customer_notes or "",
        proposed_yaml=payload.proposed_yaml or "",
        status=StorageReviewStatus.draft,
        analysis_results={},
    )
    db.add(review)
    db.commit()
    db.refresh(review)

    record_audit(
        db,
        action="storage_review.create",
        actor=request.headers.get("x-actor", "user"),
        resource_type="storage_review",
        resource_id=review.id,
        details={"name": review.name},
    )
    db.commit()
    request.state.skip_audit_log = True
    return review


@router.get("", response_model=list[StorageReviewSummary])
def list_reviews(db: Session = Depends(get_db)) -> list[dict]:
    reviews = list(
        db.scalars(
            select(StorageDesignReview)
            .options(selectinload(StorageDesignReview.findings))
            .order_by(StorageDesignReview.created_at.desc())
        ).all()
    )
    return [_summary_for(r) for r in reviews]


@router.get("/{review_id}", response_model=StorageReviewRead)
def get_review(review_id: int, db: Session = Depends(get_db)) -> StorageDesignReview:
    return _get_review_or_404(db, review_id)


@router.put("/{review_id}/notes", response_model=StorageReviewRead)
def update_notes(
    request: Request,
    review_id: int,
    payload: StorageReviewNotesUpdate,
    db: Session = Depends(get_db),
) -> StorageDesignReview:
    review = _get_review_or_404(db, review_id)
    review.customer_notes = payload.customer_notes
    db.commit()
    db.refresh(review)
    return review


@router.put("/{review_id}/yaml", response_model=StorageReviewRead)
def update_yaml(
    request: Request,
    review_id: int,
    payload: StorageReviewYamlUpdate,
    db: Session = Depends(get_db),
) -> StorageDesignReview:
    review = _get_review_or_404(db, review_id)
    review.proposed_yaml = payload.proposed_yaml
    db.commit()
    db.refresh(review)
    return review


@router.post(
    "/{review_id}/analyze",
    response_model=StorageReviewRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def analyze_review(
    request: Request,
    review_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> StorageDesignReview:
    review = _get_review_or_404(db, review_id)
    if review.status == StorageReviewStatus.analyzing:
        return review
    review.status = StorageReviewStatus.analyzing
    review.last_error = None
    db.commit()
    db.refresh(review)

    record_audit(
        db,
        action="storage_review.analyze_triggered",
        actor=request.headers.get("x-actor", "user"),
        resource_type="storage_review",
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
        action="storage_review.delete",
        actor=request.headers.get("x-actor", "user"),
        resource_type="storage_review",
        resource_id=review.id,
        details={"name": review.name},
    )
    db.delete(review)
    db.commit()
    request.state.skip_audit_log = True


@router.patch(
    "/{review_id}/findings/{finding_id}", response_model=StorageFindingRead
)
def update_finding_triage(
    request: Request,
    review_id: int,
    finding_id: int,
    payload: StorageFindingTriageUpdate,
    db: Session = Depends(get_db),
) -> StorageFinding:
    finding = db.get(StorageFinding, finding_id)
    if finding is None or finding.review_id != review_id:
        raise HTTPException(
            status_code=404,
            detail=f"Finding {finding_id} not found on review {review_id}",
        )
    finding.triage = FindingTriage(payload.triage)
    db.commit()
    db.refresh(finding)
    return finding
