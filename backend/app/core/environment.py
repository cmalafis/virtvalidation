"""Environment classification for VMs.

Federal customers consistently report that the single biggest planner
decision is "which environment does this VM belong to?" — prod / dev /
DR / infra labels drive wave assignment, target-cluster routing, and
risk posture. Without a typed environment, the planner can't safely
partition mixed-environment plans into separate MTV cutovers.

VM.environment is intentionally left as a free-text column on the VM
model (Pydantic schema validators are too strict for the imported
strings federal customers actually ship). Normalization happens via
this module:

  - ``normalize(value)`` → ``Environment`` enum (or ``UNKNOWN``).
  - ``detect_environment(vm)`` → ``(Environment, confidence)`` based
    on the 5-tier signal cascade (custom_attributes → folder → cluster
    → name prefix → DB heuristic).

The cascade is deliberately ordered so the strongest signal — an
operator-supplied custom attribute — always wins. Auto-detection
trades off completeness for accuracy: when no signal matches, the
verdict is ``UNKNOWN`` rather than a guess. Operators see ``UNKNOWN``
VMs flagged in the inventory UI and can bulk-set them.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Iterable


class Environment(str, enum.Enum):
    """Typed environment classification.

    ``UNKNOWN`` is the safe default — better than silently mis-
    classifying a VM. The planner refuses to mix environments in a
    single migration plan; UNKNOWN VMs surface in the inventory UI
    so the operator can label them before generating a plan.
    """

    PRODUCTION = "production"
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    DR = "dr"
    INFRASTRUCTURE = "infrastructure"
    DB_ONLY = "db_only"
    NON_PROD = "non_prod"
    UNKNOWN = "unknown"


# Map of free-text labels we've seen in the field to canonical enum
# values. Federal customers use a wide spectrum of phrasing in their
# RVTools custom-attributes column ("PROD", "Prod-East", "prd"); the
# normalizer accepts what they actually ship.
_ALIASES: dict[str, Environment] = {
    "prod": Environment.PRODUCTION,
    "prd": Environment.PRODUCTION,
    "production": Environment.PRODUCTION,
    "live": Environment.PRODUCTION,
    "dev": Environment.DEVELOPMENT,
    "develop": Environment.DEVELOPMENT,
    "development": Environment.DEVELOPMENT,
    "sandbox": Environment.DEVELOPMENT,
    "san": Environment.DEVELOPMENT,
    "test": Environment.TEST,
    "tst": Environment.TEST,
    "qa": Environment.TEST,
    "uat": Environment.TEST,
    "stg": Environment.STAGING,
    "stage": Environment.STAGING,
    "staging": Environment.STAGING,
    "preprod": Environment.STAGING,
    "pre-prod": Environment.STAGING,
    "dr": Environment.DR,
    "disaster": Environment.DR,
    "disasterrecovery": Environment.DR,
    "disaster-recovery": Environment.DR,
    "failover": Environment.DR,
    "infra": Environment.INFRASTRUCTURE,
    "infrastructure": Environment.INFRASTRUCTURE,
    "shared": Environment.INFRASTRUCTURE,
    "ad": Environment.INFRASTRUCTURE,
    "dc": Environment.INFRASTRUCTURE,
    "identity": Environment.INFRASTRUCTURE,
    "db": Environment.DB_ONLY,
    "database": Environment.DB_ONLY,
    "data": Environment.DB_ONLY,
    "nonprod": Environment.NON_PROD,
    "non-prod": Environment.NON_PROD,
    "non_prod": Environment.NON_PROD,
    "lower": Environment.NON_PROD,
    "unknown": Environment.UNKNOWN,
    "": Environment.UNKNOWN,
}


def normalize(value: str | None) -> Environment:
    """Map a free-text environment label to the canonical enum.

    Strips whitespace, lowercases, and looks up in ``_ALIASES``.
    Unrecognized labels fall back to ``UNKNOWN`` — better than
    silently mis-classifying.
    """
    if not value:
        return Environment.UNKNOWN
    key = re.sub(r"\s+", "", value.strip().lower())
    if not key:
        return Environment.UNKNOWN
    if key in _ALIASES:
        return _ALIASES[key]
    # Try a looser prefix match — "prod-east-01" still resolves to
    # production. Order matters; longer aliases first.
    for alias in sorted(_ALIASES, key=len, reverse=True):
        if alias and key.startswith(alias):
            return _ALIASES[alias]
    return Environment.UNKNOWN


# ---------------------------------------------------------------------------
# Detection cascade
# ---------------------------------------------------------------------------
@dataclass
class DetectionResult:
    """One environment detection outcome.

    ``confidence`` is a coarse bucket — high / medium / low — that
    operators use to triage which auto-detected labels to review.
    ``signal`` records which rule in the cascade produced the
    verdict, so future re-detection can be skipped for high-
    confidence calls when only low-confidence inputs changed.
    """

    environment: Environment
    confidence: str  # high | medium | low
    signal: str  # custom_attribute | folder | cluster | name_prefix | db_heuristic | unset


# Folder rules ordered by SPECIFICITY (longest / most-specific
# tokens first). ``/staging/prod-validation`` correctly resolves to
# STAGING because we check "/staging" before "/prod".
_FOLDER_RULES: list[tuple[str, Environment]] = [
    ("/preprod", Environment.STAGING),
    ("/staging", Environment.STAGING),
    ("/production", Environment.PRODUCTION),
    ("/disaster", Environment.DR),
    ("/identity", Environment.INFRASTRUCTURE),
    ("/develop", Environment.DEVELOPMENT),
    ("/infra", Environment.INFRASTRUCTURE),
    ("/shared", Environment.INFRASTRUCTURE),
    ("/prod", Environment.PRODUCTION),
    ("/stg", Environment.STAGING),
    ("/dev", Environment.DEVELOPMENT),
    ("/test", Environment.TEST),
    ("/uat", Environment.TEST),
    ("/qa", Environment.TEST),
    ("/dr", Environment.DR),
]

_CLUSTER_RULES: list[tuple[str, Environment]] = [
    ("prod", Environment.PRODUCTION),
    ("dev", Environment.DEVELOPMENT),
    ("test", Environment.TEST),
    ("qa", Environment.TEST),
    ("dr", Environment.DR),
    ("staging", Environment.STAGING),
    ("stg", Environment.STAGING),
    ("infra", Environment.INFRASTRUCTURE),
]

_NAME_PREFIX_RE = re.compile(r"^(prod|dev|test|stg|dr|infra|ehrstaging|ehrdr)[-_]", re.I)
_NAME_PREFIX_TO_ENV: dict[str, Environment] = {
    "prod": Environment.PRODUCTION,
    "dev": Environment.DEVELOPMENT,
    "test": Environment.TEST,
    "stg": Environment.STAGING,
    "dr": Environment.DR,
    "infra": Environment.INFRASTRUCTURE,
    "ehrstaging": Environment.STAGING,
    "ehrdr": Environment.DR,
}

# Trailing negative lookahead instead of ``\b`` so the regex catches
# "mongodb-shard" — between "mongo" and "db" there's no word boundary
# (both are letters), so a strict ``\b`` at the tail would miss it.
# ``(?![a-z])`` requires the next character (if any) to NOT be a
# lowercase letter, which accepts "-shard" / digits / end-of-string.
_DB_HEURISTIC_RE = re.compile(
    r"\b(database|postgres|mariadb|mongodb|mysql|mongo|oracle|redis|sql|db)(?![a-z])",
    re.I,
)


def detect_environment(
    *,
    name: str | None = None,
    folder_path: str | None = None,
    cluster: str | None = None,
    custom_attributes: dict | None = None,
    explicit: str | None = None,
) -> DetectionResult:
    """Five-tier signal cascade. First match wins.

    Inputs are kwargs (not the VM object) so the function stays
    pure + testable. Callers — RVTools importer, manual relabel
    endpoint, scheduled redetect job — extract the fields they
    care about and call this.

    Tier 1 — explicit operator label ("user_set" source). Wins
        over everything; we trust the operator's intent.
    Tier 2 — custom_attributes["Environment"]. The RVTools export
        carries operator-supplied custom attributes; this is the
        federal customer's authoritative classification.
    Tier 3 — folder path containing /prod, /dev, etc. vSphere
        folder hierarchy is the next-most-reliable signal.
    Tier 4 — cluster name pattern. Cluster naming tends to follow
        environment but is medium-confidence (operators reuse
        cluster names across env transitions).
    Tier 5 — VM name prefix. Lowest confidence; many VMs don't
        carry env in the name even when they're in a prod cluster.
    Tier 6 — DB heuristic. Stateful DB roles fall into DB_ONLY when
        no other signal matches; the planner treats DB_ONLY as
        "high risk + stateful" without needing a prod/dev label.

    Returns ``DetectionResult(UNKNOWN, "low", "unset")`` when
    nothing in the cascade matches. Operators see this in the
    inventory UI and bulk-label.
    """
    if explicit:
        env = normalize(explicit)
        if env != Environment.UNKNOWN:
            return DetectionResult(env, "high", "explicit")

    if custom_attributes:
        for key in ("Environment", "environment", "ENV", "env", "Env"):
            value = custom_attributes.get(key)
            if value:
                env = normalize(str(value))
                if env != Environment.UNKNOWN:
                    return DetectionResult(env, "high", "custom_attribute")

    if folder_path:
        folder = folder_path.lower()
        for token, env in _FOLDER_RULES:
            if token in folder:
                return DetectionResult(env, "high", "folder")

    if cluster:
        cluster_lc = cluster.lower()
        for token, env in _CLUSTER_RULES:
            if token in cluster_lc:
                return DetectionResult(env, "medium", "cluster")

    if name:
        m = _NAME_PREFIX_RE.match(name)
        if m:
            prefix = m.group(1).lower()
            env = _NAME_PREFIX_TO_ENV.get(prefix, Environment.UNKNOWN)
            if env != Environment.UNKNOWN:
                return DetectionResult(env, "low", "name_prefix")
        if _DB_HEURISTIC_RE.search(name):
            return DetectionResult(Environment.DB_ONLY, "low", "db_heuristic")

    return DetectionResult(Environment.UNKNOWN, "low", "unset")


def iter_supported_values() -> Iterable[str]:
    """All canonical enum string values — for API enums + dropdowns."""
    return [e.value for e in Environment]
