"""Tests for the centralized limits in app.core.limits.

The constants are load-bearing for the bulk endpoints — a regression
that drops them back to the v0.1.x 500 ceiling silently breaks every
federal customer's 1K+ RVTools upload. The tests below pin the
default values and assert env-var overrides flow through.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager


@contextmanager
def _env_override(**overrides):
    """Set env vars + reimport app.core.limits so module-level
    constants pick up the new values. Restores prior state on exit
    so other tests stay unaffected."""
    import app.core.limits as limits

    prior = {k: os.environ.get(k) for k in overrides}
    try:
        for k, v in overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        importlib.reload(limits)
        yield limits
    finally:
        for k, prev in prior.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev
        importlib.reload(limits)


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------
def test_default_constants_match_documented_values():
    """Pin the defaults so a refactor can't silently drop them."""
    import app.core.limits as limits

    assert limits.DEFAULT_PAGE_SIZE == 50
    assert limits.MAX_PAGE_SIZE == 1000
    assert limits.MAX_VMS_PER_BULK_CREATE == 10_000
    assert limits.MAX_VMS_PER_BULK_DELETE == 10_000
    assert limits.MAX_VMS_PER_BULK_ACTION == 10_000
    assert limits.MAX_VMS_PER_PLAN_SCOPE == 10_000
    assert limits.MAX_VMS_PER_RVTOOLS_IMPORT == 10_000
    assert limits.MAX_VMS_PER_CHUNK == 500
    assert limits.MAX_AUDIT_LOG_PAGE_SIZE == 500


def test_bulk_limits_are_at_or_above_1000_vm_target():
    """The whole point of this module is to support 1K+ VM federal
    fleets. Any limit below 1,000 here is a regression."""
    import app.core.limits as limits

    for name in (
        "MAX_VMS_PER_BULK_CREATE",
        "MAX_VMS_PER_BULK_DELETE",
        "MAX_VMS_PER_BULK_ACTION",
        "MAX_VMS_PER_PLAN_SCOPE",
        "MAX_VMS_PER_RVTOOLS_IMPORT",
    ):
        value = getattr(limits, name)
        assert value >= 1000, f"{name}={value} is below the 1K federal floor"


# ---------------------------------------------------------------------------
# Env-var overrides
# ---------------------------------------------------------------------------
def test_env_var_overrides_default():
    with _env_override(MAX_VMS_PER_BULK_CREATE=25_000) as limits:
        assert limits.MAX_VMS_PER_BULK_CREATE == 25_000


def test_invalid_env_var_falls_back_to_default():
    """A misconfigured ConfigMap shouldn't crash startup — fall back
    to the documented default with a warning."""
    with _env_override(MAX_VMS_PER_BULK_CREATE="not-an-integer") as limits:
        assert limits.MAX_VMS_PER_BULK_CREATE == 10_000


def test_negative_env_var_falls_back_to_default():
    with _env_override(MAX_VMS_PER_BULK_CREATE=-5) as limits:
        assert limits.MAX_VMS_PER_BULK_CREATE == 10_000


def test_zero_env_var_falls_back_to_default():
    with _env_override(MAX_VMS_PER_BULK_CREATE=0) as limits:
        assert limits.MAX_VMS_PER_BULK_CREATE == 10_000


def test_multiple_overrides_work_independently():
    with _env_override(
        DEFAULT_PAGE_SIZE=25,
        MAX_PAGE_SIZE=2000,
        MAX_VMS_PER_RVTOOLS_IMPORT=15_000,
    ) as limits:
        assert limits.DEFAULT_PAGE_SIZE == 25
        assert limits.MAX_PAGE_SIZE == 2000
        assert limits.MAX_VMS_PER_RVTOOLS_IMPORT == 15_000
        # Untouched constants keep their defaults.
        assert limits.MAX_VMS_PER_BULK_CREATE == 10_000
