"""Centralized limits for bulk operations and pagination.

These exist to prevent accidental DoS via huge requests while
accommodating realistic federal customer scale (5,000-10,000 VMs in
a single inventory). Every constant is overridable via an environment
variable so operators can dial the appliance up or down per
deployment without code changes.

Hard rules — don't violate without thinking it through:

  - Pagination defaults stay low (50-100) so first-render is fast.
  - Pagination maxes go high (1,000+) so power-users can drop to
    a single API call when scripting.
  - Bulk-action maxes (10,000) sit at the upper bound of a single
    HTTP request payload we're willing to swallow. Above that
    operators should chunk via the bulk-task endpoints (the
    chunked-import path the spec calls out for >5K VMs).
  - Audit pagination max stays low (500) — the UI surface that
    consumes it has no virtualization yet.

Federal-scale environments (DHA, FedRAMP High) routinely hit the
upper bound of these limits. Don't lower them without confirming
your customers' workloads.
"""

from __future__ import annotations

import os


def _env_int(name: str, default: int) -> int:
    """Read an int from an env var, falling back to the default.

    A bogus value (non-integer or non-positive) logs a warning and
    falls back rather than crashing the app — startup must stay
    robust against misconfigured ConfigMaps.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        # Log via stderr — logging isn't configured yet during module load.
        import sys

        sys.stderr.write(f"WARNING: {name}={raw!r} is not an integer; using default {default}\n")
        return default
    if value <= 0:
        import sys

        sys.stderr.write(f"WARNING: {name}={value} must be positive; using default {default}\n")
        return default
    return value


# ---------------------------------------------------------------------------
# Pagination — what the listing endpoints' ``limit`` query params accept.
# Two values per endpoint family: a sensible default for first-render
# and a hard cap so a bug in a paging loop can't OOM the worker.
# ---------------------------------------------------------------------------

#: Default page size when the caller doesn't specify ``?limit=``.
DEFAULT_PAGE_SIZE: int = _env_int("DEFAULT_PAGE_SIZE", 50)

#: Hard cap on ``?limit=`` for listing endpoints. 1,000 covers the
#: largest single-page renders the UI does today (the inventory
#: page) and lets scripts pull a whole vCenter inventory in one
#: round-trip.
MAX_PAGE_SIZE: int = _env_int("MAX_PAGE_SIZE", 1000)


# ---------------------------------------------------------------------------
# Bulk-operation input limits — what payload sizes the bulk POST
# endpoints accept. Sized for federal scale (DHA's typical fleet is
# 3K-8K VMs per vCenter; the appliance handles up to 10K in one
# request).
# ---------------------------------------------------------------------------

#: Max items in ``POST /api/vms/bulk`` body.
MAX_VMS_PER_BULK_CREATE: int = _env_int("MAX_VMS_PER_BULK_CREATE", 10_000)

#: Max items in ``DELETE /api/vms`` body.
MAX_VMS_PER_BULK_DELETE: int = _env_int("MAX_VMS_PER_BULK_DELETE", 10_000)

#: Generic ceiling for bulk-action endpoints (capture / validate /
#: scope filters). Keep aligned with the create/delete caps unless a
#: specific endpoint has a stronger reason to differ.
MAX_VMS_PER_BULK_ACTION: int = _env_int("MAX_VMS_PER_BULK_ACTION", 10_000)

#: Max items in a plan-scope filter's ``vm_ids`` field.
MAX_VMS_PER_PLAN_SCOPE: int = _env_int("MAX_VMS_PER_PLAN_SCOPE", 10_000)

#: Max items in an RVTools auto-link upload's ``vms`` field. Sized to
#: match the bulk-create cap — one RVTools file should never exceed
#: a single bulk request.
MAX_VMS_PER_RVTOOLS_IMPORT: int = _env_int("MAX_VMS_PER_RVTOOLS_IMPORT", 10_000)

#: Batch size for the server-side streaming importer
#: (``app.core.import_jobs``): VMs upserted per transaction. One commit
#: per batch means a late failure keeps the completed batches.
MAX_VMS_PER_CHUNK: int = _env_int("MAX_VMS_PER_CHUNK", 500)

#: Largest inventory file the upload endpoint will spool to disk. A
#: 5,000-VM all-sheets RVTools export is ~10 MB; this leaves headroom
#: without letting a mis-click fill the pod's emptyDir.
MAX_IMPORT_UPLOAD_BYTES: int = _env_int("MAX_IMPORT_UPLOAD_BYTES", 100 * 1024 * 1024)


# ---------------------------------------------------------------------------
# Audit-log pagination — kept low because the audit UI doesn't have
# virtualization yet. Bumping this above 500 makes the timeline
# render time-quadratic.
# ---------------------------------------------------------------------------
MAX_AUDIT_LOG_PAGE_SIZE: int = _env_int("MAX_AUDIT_LOG_PAGE_SIZE", 500)


__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "MAX_VMS_PER_BULK_CREATE",
    "MAX_VMS_PER_BULK_DELETE",
    "MAX_VMS_PER_BULK_ACTION",
    "MAX_VMS_PER_PLAN_SCOPE",
    "MAX_VMS_PER_RVTOOLS_IMPORT",
    "MAX_VMS_PER_CHUNK",
    "MAX_IMPORT_UPLOAD_BYTES",
    "MAX_AUDIT_LOG_PAGE_SIZE",
]
