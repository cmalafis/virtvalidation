"""Diff-keyed cache for LLM validation verdicts.

Homogeneous infrastructure produces identical structured diffs across
many VMs — a fleet of 50 web servers migrated by the same wave will
typically produce the same "kubevirt-agent added, eth0→ens192
rename, MTU 1500→9000" delta on every host. The first VM's LLM
verdict applies verbatim to every subsequent VM with the same diff.

Cache key: SHA-256 of the canonical-JSON-encoded diff, scoped by the
VM's OS family (a "service X stopped" diff means different things on
Linux vs Windows so we never share verdicts across OS families). TTL
defaults to 7 days — long enough to cover a single weekend cutover
wave, short enough that stale verdicts age out before the next
migration cycle.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType


class ValidationLLMCache(Base):
    __tablename__ = "validation_llm_cache"

    id: Mapped[int] = mapped_column(primary_key=True)

    # SHA-256 hash of the canonical-JSON-encoded diff + os_family.
    # 64-char hex string; indexed for the cache-lookup hot path.
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)

    # The OS family the verdict was produced for. Stored separately
    # so cache rolls-ups in the admin dashboard can break down hit
    # rates by platform.
    os_family: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")

    # Verdict shape mirrors what the LLM client emits + what
    # ValidationResult persists: status / summary / findings /
    # remediation. Cached verbatim so cache hits skip the LLM call
    # entirely.
    verdict: Mapped[dict] = mapped_column(JSONType, nullable=False)

    # Bookkeeping. hit_count drives the admin dashboard's cache
    # hit-rate metric; last_hit_at tells operators when a cached
    # entry was last reused.
    hit_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    last_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The first VM that produced this verdict. Useful for federal
    # reviewers tracing where a cached verdict originally came from.
    source_vm_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diff_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Hard expiry — entries older than this are deleted on next
    # eviction sweep regardless of hit_count.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_validation_llm_cache_key", "cache_key"),
        Index("ix_validation_llm_cache_expires", "expires_at"),
    )
