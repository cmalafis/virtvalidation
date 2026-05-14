"""Tests for Environment normalization + the detection cascade.

Pins the 5-tier order — operator-supplied custom_attributes always
win over heuristic name-prefix matching, etc. — so the auto-detect
runs in the RVTools importer can't accidentally regress on a small
free-text variation in customer data.
"""

from __future__ import annotations

import pytest

from app.core.environment import (
    DetectionResult,
    Environment,
    detect_environment,
    normalize,
)


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("production", Environment.PRODUCTION),
        ("PRODUCTION", Environment.PRODUCTION),
        ("Prod", Environment.PRODUCTION),
        ("prd", Environment.PRODUCTION),
        ("live", Environment.PRODUCTION),
        ("development", Environment.DEVELOPMENT),
        ("dev", Environment.DEVELOPMENT),
        ("Dev-Server", Environment.DEVELOPMENT),
        ("sandbox", Environment.DEVELOPMENT),
        ("QA", Environment.TEST),
        ("uat", Environment.TEST),
        ("Test", Environment.TEST),
        ("staging", Environment.STAGING),
        ("STG", Environment.STAGING),
        ("preprod", Environment.STAGING),
        ("pre-prod", Environment.STAGING),
        ("DR", Environment.DR),
        ("disaster", Environment.DR),
        ("disaster-recovery", Environment.DR),
        ("Failover", Environment.DR),
        ("Infra", Environment.INFRASTRUCTURE),
        ("INFRASTRUCTURE", Environment.INFRASTRUCTURE),
        ("DB", Environment.DB_ONLY),
        ("Database", Environment.DB_ONLY),
        ("non-prod", Environment.NON_PROD),
        ("NonProd", Environment.NON_PROD),
        ("", Environment.UNKNOWN),
        ("   ", Environment.UNKNOWN),
        ("randomString", Environment.UNKNOWN),
        (None, Environment.UNKNOWN),
    ],
)
def test_normalize_handles_field_variants(raw, expected):
    assert normalize(raw) == expected


def test_normalize_prefix_match_with_trailing_garbage():
    # "prod-east-01" should still resolve to PRODUCTION.
    assert normalize("prod-east-01") == Environment.PRODUCTION
    assert normalize("dr-region-2") == Environment.DR


# ---------------------------------------------------------------------------
# Detection cascade — Tier 1 (explicit operator label)
# ---------------------------------------------------------------------------
def test_explicit_user_label_wins():
    result = detect_environment(
        name="dev-server-01",  # would otherwise → DEVELOPMENT
        explicit="production",  # operator override
    )
    assert result.environment == Environment.PRODUCTION
    assert result.confidence == "high"
    assert result.signal == "explicit"


# ---------------------------------------------------------------------------
# Detection cascade — Tier 2 (custom_attributes)
# ---------------------------------------------------------------------------
def test_custom_attribute_environment_wins_over_folder():
    result = detect_environment(
        name="ehrpro-web-01",
        folder_path="/dev/sandbox",  # would say DEVELOPMENT
        custom_attributes={"Environment": "Production"},
    )
    assert result.environment == Environment.PRODUCTION
    assert result.confidence == "high"
    assert result.signal == "custom_attribute"


def test_custom_attribute_handles_lowercase_key():
    result = detect_environment(
        custom_attributes={"environment": "DR"},
    )
    assert result.environment == Environment.DR


def test_custom_attribute_with_unknown_value_falls_through():
    # Operator typo'd "Wibble" — fall through to other signals.
    result = detect_environment(
        name="prod-server-01",
        custom_attributes={"Environment": "Wibble"},
    )
    assert result.environment == Environment.PRODUCTION  # name match
    assert result.signal == "name_prefix"


# ---------------------------------------------------------------------------
# Detection cascade — Tier 3 (folder)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "folder,expected",
    [
        ("/prod/ehr-pro", Environment.PRODUCTION),
        ("/Dev/Sandbox", Environment.DEVELOPMENT),
        ("/qa/uat-1", Environment.TEST),
        ("/dr/replica", Environment.DR),
        ("/infrastructure/ad-dc", Environment.INFRASTRUCTURE),
        ("/staging/prod-validation", Environment.STAGING),
    ],
)
def test_folder_path_signal(folder, expected):
    result = detect_environment(folder_path=folder)
    assert result.environment == expected
    assert result.confidence == "high"
    assert result.signal == "folder"


def test_folder_path_no_match_falls_through():
    result = detect_environment(folder_path="/root/random")
    assert result.environment == Environment.UNKNOWN
    assert result.signal == "unset"


# ---------------------------------------------------------------------------
# Detection cascade — Tier 4 (cluster name)
# ---------------------------------------------------------------------------
def test_cluster_name_signal():
    result = detect_environment(cluster="prod-us-east-cluster-01")
    assert result.environment == Environment.PRODUCTION
    assert result.confidence == "medium"
    assert result.signal == "cluster"


def test_cluster_loses_to_folder():
    result = detect_environment(
        cluster="prod-east",
        folder_path="/dev/sandbox",
    )
    # Folder wins (higher tier).
    assert result.environment == Environment.DEVELOPMENT
    assert result.signal == "folder"


# ---------------------------------------------------------------------------
# Detection cascade — Tier 5 (name prefix)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [
        ("prod-web-01", Environment.PRODUCTION),
        ("dev-server-01", Environment.DEVELOPMENT),
        ("test-runner-1", Environment.TEST),
        ("stg-app-01", Environment.STAGING),
        ("dr-replica-01", Environment.DR),
        ("infra-shared-01", Environment.INFRASTRUCTURE),
    ],
)
def test_name_prefix_signal(name, expected):
    result = detect_environment(name=name)
    assert result.environment == expected
    assert result.confidence == "low"
    assert result.signal == "name_prefix"


# ---------------------------------------------------------------------------
# Detection cascade — Tier 6 (DB heuristic)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name",
    [
        "postgres-replica-01",
        "mysql-cluster",
        "mongodb-shard",
        "oracle-prod-db",
        "redis-cache-1",
    ],
)
def test_db_heuristic_when_no_other_signal(name):
    result = detect_environment(name=name)
    assert result.environment == Environment.DB_ONLY
    assert result.confidence == "low"
    assert result.signal == "db_heuristic"


def test_db_heuristic_loses_to_name_prefix():
    # "prod-postgres-01" matches name_prefix first (prod-).
    result = detect_environment(name="prod-postgres-01")
    assert result.environment == Environment.PRODUCTION
    assert result.signal == "name_prefix"


# ---------------------------------------------------------------------------
# Cascade ordering — higher-tier signal always wins
# ---------------------------------------------------------------------------
def test_cascade_ordering():
    # All five signals point at different envs. Tier 1 (explicit)
    # wins.
    result = detect_environment(
        explicit="staging",
        custom_attributes={"Environment": "Production"},
        folder_path="/dev/sandbox",
        cluster="dr-cluster",
        name="prod-server-01",
    )
    assert result.environment == Environment.STAGING
    assert result.signal == "explicit"


def test_cascade_falls_through_to_unknown():
    result = detect_environment(
        name="alpha",
        folder_path="/random",
        cluster="generic-cluster",
        custom_attributes={"Owner": "team@example.com"},
    )
    assert result.environment == Environment.UNKNOWN
    assert result.signal == "unset"


# ---------------------------------------------------------------------------
# DetectionResult shape
# ---------------------------------------------------------------------------
def test_detection_result_fields_are_strings():
    result = detect_environment(name="prod-web-01")
    assert isinstance(result, DetectionResult)
    assert result.environment.value == "production"
    assert result.confidence in {"high", "medium", "low"}
    assert result.signal
