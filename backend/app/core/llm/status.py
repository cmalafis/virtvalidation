"""Last-observed LLM error — written to ``app_settings.last_llm_error``
so the Settings UI can surface auth-failure banners.

Why this lives in its own module:

- Several call sites (planner annotation, validation client, future
  reviewers) need to write the same record. Keeping a single helper
  prevents ``last_llm_error`` strings from drifting in wording.
- Writing the status MUST NOT mask the original LLM failure. Any DB
  error in here is logged and swallowed — the caller's mechanical
  fallback continues unaffected.
- API key / token material MUST NEVER reach this module. Callers
  build the ``message`` string from the URL + status only; the
  helper double-checks that no obvious credential pattern slipped in
  via ``str(exc)``.

The Settings UI clears the banner on a successful test_connection
or any successful LLM call. The clear path is symmetric — a
fix-and-retry should make the warning disappear without the operator
having to dismiss it manually.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from sqlalchemy.exc import SQLAlchemyError

from app.core import db as _db_module

logger = logging.getLogger(__name__)


# Cheap pattern guard against accidentally including raw credential
# material in a status message. Matches "Bearer <token>" /
# "api_key=..." / "authorization: ..." style strings. Conservative
# false-positive rate; if it ever fires in production the right move
# is to fix the caller, not loosen the regex.
_CREDENTIAL_PATTERN = re.compile(
    r"(bearer\s+\S{6,}|api[_-]?key\s*=\s*\S{6,}|authorization:\s*\S+)",
    re.IGNORECASE,
)


def _scrub(message: str) -> str:
    """Replace any credential-shaped substring with ``****``."""
    return _CREDENTIAL_PATTERN.sub("****", message)


def record_last_llm_error(message: str) -> None:
    """Write ``message`` to ``app_settings.last_llm_error`` and stamp
    ``last_llm_error_at`` with the current UTC time. Errors are
    logged and swallowed — the caller's flow continues."""
    safe = _scrub(message or "")
    try:
        from app.models.settings import AppSettings  # avoid circular import

        session = _db_module.SessionLocal()
        try:
            row = session.get(AppSettings, 1)
            if row is None:
                row = AppSettings(
                    id=1, last_llm_error=safe, last_llm_error_at=datetime.now(timezone.utc)
                )
                session.add(row)
            else:
                row.last_llm_error = safe
                row.last_llm_error_at = datetime.now(timezone.utc)
            session.commit()
        finally:
            session.close()
    except SQLAlchemyError as exc:
        logger.warning("status.record_last_llm_error db_error=%s", exc)
    except Exception as exc:  # noqa: BLE001 — must never propagate
        logger.warning("status.record_last_llm_error failed=%s", exc)


def clear_last_llm_error() -> None:
    """Clear the banner — called on any successful LLM call so a stale
    auth-failure message doesn't linger after the operator fixed the
    key."""
    try:
        from app.models.settings import AppSettings  # avoid circular import

        session = _db_module.SessionLocal()
        try:
            row = session.get(AppSettings, 1)
            if row is None or row.last_llm_error is None:
                return  # nothing to clear
            row.last_llm_error = None
            row.last_llm_error_at = None
            session.commit()
        finally:
            session.close()
    except SQLAlchemyError as exc:
        logger.warning("status.clear_last_llm_error db_error=%s", exc)
    except Exception as exc:  # noqa: BLE001 — must never propagate
        logger.warning("status.clear_last_llm_error failed=%s", exc)
