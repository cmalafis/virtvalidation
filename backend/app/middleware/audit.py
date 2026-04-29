"""Starlette middleware that records every mutating API call into audit_logs."""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from app.core import db as _db_module  # late binding so tests can rebind SessionLocal
from app.core.audit import infer_action, record_audit

logger = logging.getLogger(__name__)


class AuditMiddleware(BaseHTTPMiddleware):
    """Log POST/PUT/PATCH/DELETE responses to the audit_logs table.

    Reads (GET/HEAD/OPTIONS) are not recorded here — endpoints that need
    GET-side audit (e.g. PDF export) call record_audit() directly.

    The audit endpoint itself is skipped to avoid noisy self-references.
    """

    LOG_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if request.method not in self.LOG_METHODS:
            return response
        path = request.url.path
        if path.startswith("/audit"):
            return response

        try:
            action, rtype, rid = infer_action(request.method, path)
            actor = request.headers.get("x-actor", "anonymous")
            details = {
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
            }
            session = _db_module.SessionLocal()
            try:
                record_audit(
                    session,
                    action=action,
                    actor=actor,
                    resource_type=rtype,
                    resource_id=rid,
                    details=details,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()
        except Exception:  # pragma: no cover - audit failures must never break a request
            logger.exception("audit middleware failed for %s %s", request.method, path)

        return response
