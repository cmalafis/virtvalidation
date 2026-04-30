"""Audit-trail recording helpers.

The middleware writes a row for every mutating API call; modules that own
non-API actions (the scheduler, GET-side report exports) call
`record_audit()` directly so the trail stays complete.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy.orm import Session

from app.models.audit import AuditLog

# (method, compiled regex, action, resource_type, group index for resource_id)
_ACTIONS: list[tuple[str, re.Pattern[str], str, str | None, int | None]] = [
    ("POST", re.compile(r"^/api/vms/?$"), "vm.create", "vm", None),
    ("POST", re.compile(r"^/api/vms/bulk/?$"), "vm.bulk_create", "vm", None),
    ("DELETE", re.compile(r"^/api/vms/?$"), "vm.bulk_delete", "vm", None),
    ("PATCH", re.compile(r"^/api/vms/(\d+)/?$"), "vm.update", "vm", 1),
    ("DELETE", re.compile(r"^/api/vms/(\d+)/?$"), "vm.delete", "vm", 1),
    ("POST", re.compile(r"^/api/vms/(\d+)/snapshots/?$"), "baseline.create", "baseline", 1),
    ("POST", re.compile(r"^/api/vms/(\d+)/capture/?$"), "capture.triggered", "vm", 1),
    ("POST", re.compile(r"^/api/snapshots/capture-all/?$"), "capture.bulk_triggered", "vm", None),
    # ssh.host_key_* rows are emitted directly by the capture wrapper —
    # no middleware-side inference needed; they're listed below for the
    # frontend filter dropdown.
    ("POST", re.compile(r"^/api/plans/?$"), "plan.create", "plan", None),
    ("PUT", re.compile(r"^/api/settings/?$"), "settings.update", "settings", None),
]


def infer_action(method: str, path: str) -> tuple[str, str | None, str | None]:
    """Map (method, path) → (action, resource_type, resource_id).

    Falls back to a generic ``api.<method>`` action so unmatched mutating
    calls still produce a row.
    """
    for m, pat, action, rtype, idg in _ACTIONS:
        if m != method:
            continue
        mt = pat.match(path)
        if mt:
            rid = mt.group(idg) if idg is not None else None
            return action, rtype, rid
    return f"api.{method.lower()}", None, None


def record_audit(
    db: Session,
    *,
    action: str,
    actor: str = "system",
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Stage an audit log row. The caller is responsible for ``db.commit()``."""
    row = AuditLog(
        action=action,
        actor=actor,
        resource_type=resource_type,
        resource_id=str(resource_id) if resource_id is not None else None,
        details=details or {},
    )
    db.add(row)
    return row
