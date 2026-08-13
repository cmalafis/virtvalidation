"""Read-only inference-log endpoints.

Surfaces the full LLM input/output capture (:class:`InferenceLog`) so
operators can audit exactly what the agent asked the model and what it
returned — and the ``/stats`` roll-up so they can spot when the fallback
paths are firing (degraded LLM quality, auth, or a guardrail block).

Append-only by design — there is no create/update/delete here; rows are
written by the orchestrators via ``app.core.llm.inference_log.record_inference``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.limits import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.models.inference_log import InferenceLog
from app.schemas.inference_log import (
    InferenceLogListResponse,
    InferenceLogStats,
)

router = APIRouter(tags=["inference-logs"])


def _apply_filters(stmt, *, operation, method, backend_type, vm_id):
    if operation:
        stmt = stmt.where(InferenceLog.operation.in_(operation))
    if method:
        stmt = stmt.where(InferenceLog.method.in_(method))
    if backend_type:
        stmt = stmt.where(InferenceLog.backend_type.in_(backend_type))
    if vm_id is not None:
        stmt = stmt.where(InferenceLog.vm_id == vm_id)
    return stmt


@router.get("", response_model=InferenceLogListResponse)
def list_inference_logs(
    db: Session = Depends(get_db),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    operation: list[str] | None = Query(default=None),
    method: list[str] | None = Query(default=None),
    backend_type: list[str] | None = Query(default=None),
    vm_id: int | None = Query(default=None),
) -> dict:
    """Paginated, filterable inference-log listing — newest first.

    Returns ``{items, total, skip, limit}``; ``total`` is the count after
    filters but before pagination, so the UI can render "Showing X-Y of Z".
    """
    base = _apply_filters(
        select(InferenceLog),
        operation=operation,
        method=method,
        backend_type=backend_type,
        vm_id=vm_id,
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = (
        base.order_by(InferenceLog.created_at.desc(), InferenceLog.id.desc())
        .limit(limit)
        .offset(skip)
    )
    items = list(db.scalars(page).all())
    return {"items": items, "total": total, "skip": skip, "limit": limit}


@router.get("/stats", response_model=InferenceLogStats)
def inference_log_stats(
    db: Session = Depends(get_db),
    operation: list[str] | None = Query(default=None),
    method: list[str] | None = Query(default=None),
    backend_type: list[str] | None = Query(default=None),
    vm_id: int | None = Query(default=None),
) -> dict:
    """Roll-up counts by method and operation — feeds the LLM-health panel."""
    filt = dict(operation=operation, method=method, backend_type=backend_type, vm_id=vm_id)
    total = (
        db.scalar(
            select(func.count()).select_from(
                _apply_filters(select(InferenceLog), **filt).subquery()
            )
        )
        or 0
    )
    by_method = {
        row[0]: row[1]
        for row in db.execute(
            _apply_filters(
                select(InferenceLog.method, func.count()).group_by(InferenceLog.method),
                **filt,
            )
        ).all()
    }
    by_operation = {
        row[0]: row[1]
        for row in db.execute(
            _apply_filters(
                select(InferenceLog.operation, func.count()).group_by(InferenceLog.operation),
                **filt,
            )
        ).all()
    }
    return {"total": total, "by_method": by_method, "by_operation": by_operation}
