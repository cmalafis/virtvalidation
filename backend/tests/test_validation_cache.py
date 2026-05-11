"""Tests for the diff-hash LLM-response cache.

The cache is the second efficiency lever: identical diffs across many
VMs (50 web servers all migrated by the same wave produce the same
delta) should share one LLM verdict. Failures here either waste LLM
calls (cache misses for what should be hits) or — worse — return
stale verdicts past their TTL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.validation_cache import (
    compute_cache_key,
    evict_expired,
    lookup,
    stats,
    store,
)
from app.models.validation_cache import ValidationLLMCache


def _diff(extra_added=None) -> dict:
    return {
        "services": {
            "added": ["kubevirt-agent.service"] + list(extra_added or []),
            "removed": [],
        },
        "ports": {"added": [], "removed": []},
        "mounts": {"added": [], "removed": [], "changed": []},
        "network": {"interfaces": {}, "routes_added": [], "routes_removed": [],
                     "dns_added": [], "dns_removed": []},
        "cron": {},
    }


def _verdict(status="pass", summary="ok") -> dict:
    return {
        "status": status,
        "summary": summary,
        "findings": [],
        "remediation": [],
        "confidence": "high",
    }


# ---------------------------------------------------------------------------
# Key + identity
# ---------------------------------------------------------------------------
def test_identical_diffs_share_cache_key():
    a = _diff()
    b = _diff()
    assert compute_cache_key(a, "rhel-like") == compute_cache_key(b, "rhel-like")


def test_different_diffs_have_different_keys():
    a = _diff()
    b = _diff(extra_added=["custom.service"])
    assert compute_cache_key(a, "rhel-like") != compute_cache_key(b, "rhel-like")


def test_same_diff_different_os_family_have_different_keys():
    """A 'service X stopped' diff means different recovery steps on
    Linux vs Windows. Verdicts must NOT cross OS families."""
    a = _diff()
    assert compute_cache_key(a, "rhel-like") != compute_cache_key(a, "windows")


def test_dict_insertion_order_does_not_affect_key():
    """JSON encoding must sort keys; otherwise identical-content diffs
    constructed in different orders would miss the cache."""
    a = {"services": {"added": [], "removed": []}, "ports": {}}
    b = {"ports": {}, "services": {"removed": [], "added": []}}
    assert compute_cache_key(a, "rhel-like") == compute_cache_key(b, "rhel-like")


# ---------------------------------------------------------------------------
# Store / lookup
# ---------------------------------------------------------------------------
def test_lookup_returns_none_when_no_entry(db_session):
    result = lookup(db_session, diff=_diff(), os_family="rhel-like")
    assert result is None


def test_stored_verdict_is_returned_on_lookup(db_session):
    diff = _diff()
    store(
        db_session,
        diff=diff,
        os_family="rhel-like",
        verdict=_verdict(),
        source_vm_id=1,
        source_model="test-model",
    )
    hit = lookup(db_session, diff=diff, os_family="rhel-like")
    assert hit is not None
    assert hit["status"] == "pass"
    assert hit["cached"] is True
    assert "cache_key" in hit


def test_lookup_increments_hit_count(db_session):
    diff = _diff()
    store(
        db_session,
        diff=diff,
        os_family="rhel-like",
        verdict=_verdict(),
        source_vm_id=1,
        source_model="m",
    )
    lookup(db_session, diff=diff, os_family="rhel-like")
    lookup(db_session, diff=diff, os_family="rhel-like")
    lookup(db_session, diff=diff, os_family="rhel-like")
    row = db_session.query(ValidationLLMCache).first()
    assert row.hit_count == 3
    assert row.last_hit_at is not None


def test_store_is_idempotent_on_same_key(db_session):
    diff = _diff()
    store(
        db_session,
        diff=diff,
        os_family="rhel-like",
        verdict=_verdict(summary="first"),
        source_vm_id=1,
        source_model="m",
    )
    store(
        db_session,
        diff=diff,
        os_family="rhel-like",
        verdict=_verdict(summary="second"),
        source_vm_id=2,
        source_model="m",
    )
    rows = db_session.query(ValidationLLMCache).all()
    assert len(rows) == 1
    assert rows[0].verdict["summary"] == "second"


def test_store_strips_marker_fields_from_persisted_verdict(db_session):
    """A verdict round-tripped through ``lookup`` carries ``cached`` /
    ``cache_key`` / ``tier`` markers — those must NOT be persisted on
    re-store or they'd accumulate."""
    diff = _diff()
    polluted = {**_verdict(), "cached": True, "cache_key": "x", "tier": "tier3"}
    store(
        db_session,
        diff=diff,
        os_family="rhel-like",
        verdict=polluted,
        source_vm_id=1,
        source_model="m",
    )
    row = db_session.query(ValidationLLMCache).first()
    for marker in ("cached", "cache_key", "tier"):
        assert marker not in row.verdict


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------
def test_expired_entry_is_not_returned(db_session):
    diff = _diff()
    store(
        db_session,
        diff=diff,
        os_family="rhel-like",
        verdict=_verdict(),
        source_vm_id=1,
        source_model="m",
    )
    # Force the entry into the past.
    row = db_session.query(ValidationLLMCache).first()
    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    assert lookup(db_session, diff=diff, os_family="rhel-like") is None


def test_evict_expired_removes_only_expired_rows(db_session):
    fresh = _diff()
    stale = _diff(extra_added=["older.service"])
    store(db_session, diff=fresh, os_family="rhel-like",
          verdict=_verdict(), source_vm_id=1, source_model="m")
    store(db_session, diff=stale, os_family="rhel-like",
          verdict=_verdict(), source_vm_id=2, source_model="m")
    stale_row = (
        db_session.query(ValidationLLMCache)
        .filter(ValidationLLMCache.diff_summary.like("services_added=2%"))
        .one()
    )
    stale_row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db_session.commit()

    deleted = evict_expired(db_session)
    assert deleted == 1
    remaining = db_session.query(ValidationLLMCache).all()
    assert len(remaining) == 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def test_stats_aggregates_hits_per_os_family(db_session):
    diff_a = _diff()
    diff_b = _diff(extra_added=["x.service"])
    store(db_session, diff=diff_a, os_family="rhel-like",
          verdict=_verdict(), source_vm_id=1, source_model="m")
    store(db_session, diff=diff_b, os_family="windows",
          verdict=_verdict(), source_vm_id=2, source_model="m")
    lookup(db_session, diff=diff_a, os_family="rhel-like")
    lookup(db_session, diff=diff_a, os_family="rhel-like")

    summary = stats(db_session)
    assert summary["entries"] == 2
    assert summary["total_hits"] == 2
    assert summary["by_os_family"]["rhel-like"]["entries"] == 1
    assert summary["by_os_family"]["rhel-like"]["hits"] == 2
    assert summary["by_os_family"]["windows"]["hits"] == 0
