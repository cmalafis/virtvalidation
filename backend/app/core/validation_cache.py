"""Cache lookups + writes for :class:`ValidationLLMCache`.

The cache key is the SHA-256 of a deterministic JSON encoding of the
structured diff, namespaced by OS family. Determinism matters: the
diff dicts emitted by ``LLMClient._diff_state`` already produce sorted
lists, but the JSON encoding must also sort dict keys so two diffs
with the same content but different insertion order map to the same
key.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.validation_cache import ValidationLLMCache

logger = logging.getLogger(__name__)


# 7 days — long enough for a typical migration weekend + the
# following week of post-cutover validation runs; short enough that
# stale verdicts age out before the next migration cycle.
DEFAULT_TTL = timedelta(days=7)


def compute_cache_key(diff: dict, os_family: str | None) -> str:
    """SHA-256 of canonical-JSON-encoded ``(diff, os_family)``.

    The same diff on Linux and Windows VMs gets different cache keys
    because the recommended remediation differs by platform.
    """
    payload = {
        "os_family": (os_family or "unknown").lower(),
        "diff": diff,
    }
    serialized = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def lookup(
    db: Session, *, diff: dict, os_family: str | None
) -> Optional[dict]:
    """Return the cached verdict for this diff, or ``None``.

    Increments ``hit_count`` and stamps ``last_hit_at`` on the row
    when a hit is found so the admin dashboard can render hit rates.
    Returns ``None`` when no row exists or the row has expired.
    """
    key = compute_cache_key(diff, os_family)
    row = db.scalars(
        select(ValidationLLMCache).where(ValidationLLMCache.cache_key == key)
    ).first()
    if row is None:
        return None
    # SQLite returns naive datetimes; normalize to UTC so the
    # comparison works across backends.
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        # Lazy eviction — the row will be re-created on the next miss,
        # and the periodic sweep cleans up everything else.
        logger.info("Cache entry %s expired (created %s)", key[:12], row.created_at)
        return None
    row.hit_count = (row.hit_count or 0) + 1
    row.last_hit_at = datetime.now(timezone.utc)
    db.commit()
    verdict = dict(row.verdict or {})
    verdict["cached"] = True
    verdict["cache_key"] = key
    return verdict


def store(
    db: Session,
    *,
    diff: dict,
    os_family: str | None,
    verdict: dict,
    source_vm_id: int | None,
    source_model: str | None,
    ttl: timedelta | None = None,
) -> None:
    """Persist an LLM verdict for future cache hits.

    Idempotent on the diff/os_family pair — re-storing the same key
    updates the verdict + extends the expiry rather than crashing on
    the unique constraint.
    """
    if not verdict:
        return
    key = compute_cache_key(diff, os_family)
    ttl = ttl or DEFAULT_TTL
    expires = datetime.now(timezone.utc) + ttl

    # Strip the cache-marker fields if a caller round-tripped a hit
    # back through us; we don't want them in the persisted verdict.
    stripped = {k: v for k, v in verdict.items() if k not in {"cached", "cache_key", "tier"}}

    existing = db.scalars(
        select(ValidationLLMCache).where(ValidationLLMCache.cache_key == key)
    ).first()
    if existing is not None:
        existing.verdict = stripped
        existing.source_model = source_model or existing.source_model
        existing.expires_at = expires
        db.commit()
        return

    row = ValidationLLMCache(
        cache_key=key,
        os_family=(os_family or "unknown").lower(),
        verdict=stripped,
        source_vm_id=source_vm_id,
        source_model=source_model,
        diff_summary=_summarize_diff(diff),
        expires_at=expires,
    )
    db.add(row)
    db.commit()


def evict_expired(db: Session) -> int:
    """Delete expired rows. Returns the deleted count.

    Called from the scheduler's periodic sweep. SQLite stores naive
    datetimes so we walk rows manually rather than filter in SQL —
    the row count is small (cache entries cap at hundreds in practice).
    """
    now = datetime.now(timezone.utc)
    deleted = 0
    for row in db.scalars(select(ValidationLLMCache)).all():
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= now:
            db.delete(row)
            deleted += 1
    db.commit()
    return deleted


def stats(db: Session) -> dict:
    """Aggregate cache statistics for the admin dashboard."""
    rows = list(db.scalars(select(ValidationLLMCache)).all())
    if not rows:
        return {
            "entries": 0,
            "total_hits": 0,
            "by_os_family": {},
        }
    total_hits = sum(r.hit_count or 0 for r in rows)
    by_family: dict[str, dict] = {}
    for r in rows:
        bucket = by_family.setdefault(
            r.os_family or "unknown", {"entries": 0, "hits": 0}
        )
        bucket["entries"] += 1
        bucket["hits"] += r.hit_count or 0
    return {
        "entries": len(rows),
        "total_hits": total_hits,
        "by_os_family": by_family,
    }


def _summarize_diff(diff: dict) -> str:
    """One-line summary persisted with the cache row for review.

    Federal reviewers reading the cache table want to know what a
    given entry represents without unpacking the full JSON diff.
    """
    parts: list[str] = []
    services = diff.get("services") or {}
    added = services.get("added") or []
    removed = services.get("removed") or []
    if added:
        parts.append(f"services_added={len(added)}")
    if removed:
        parts.append(f"services_removed={len(removed)}")
    network = diff.get("network") or {}
    if network.get("interfaces"):
        parts.append(f"network_changed={len(network['interfaces'])}")
    return "; ".join(parts) or "no-op"
