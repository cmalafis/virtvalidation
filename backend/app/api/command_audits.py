"""Read-only per-command SSH audit endpoint.

Surfaces :class:`CommandAudit` so operators can see exactly what the agent ran
on each production host — command, exit status, stdout hash + preview, timing,
and whether the read-only gate blocked it. Append-only; rows are written by the
collection orchestrator (``app.core.collection.wave_jobs``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.limits import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.models.command_audit import CommandAudit
from app.schemas.command_audit import CommandAuditListResponse

router = APIRouter(tags=["command-audits"])


@router.get("", response_model=CommandAuditListResponse)
def list_command_audits(
    db: Session = Depends(get_db),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    run_type: list[str] | None = Query(default=None),
    run_id: int | None = Query(default=None),
    vm_id: int | None = Query(default=None),
    blocked: bool | None = Query(default=None),
) -> dict:
    """Paginated, filterable command-audit listing — newest first.

    Returns ``{items, total, skip, limit}``. ``blocked=true`` isolates the
    security-relevant rows where the read-only gate refused a command.
    """
    base = select(CommandAudit)
    if run_type:
        base = base.where(CommandAudit.run_type.in_(run_type))
    if run_id is not None:
        base = base.where(CommandAudit.run_id == run_id)
    if vm_id is not None:
        base = base.where(CommandAudit.vm_id == vm_id)
    if blocked is not None:
        base = base.where(CommandAudit.blocked.is_(blocked))

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    page = (
        base.order_by(CommandAudit.started_at.desc(), CommandAudit.id.desc())
        .limit(limit)
        .offset(skip)
    )
    items = list(db.scalars(page).all())
    return {"items": items, "total": total, "skip": skip, "limit": limit}
