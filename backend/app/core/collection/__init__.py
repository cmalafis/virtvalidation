"""Deterministic SSH collection engine for the wave-scoped flow.

Public types:
  - :class:`CollectionEngine` — single-VM collector wrapping ``SSHCollector``.
  - :class:`CollectionResult` — per-VM outcome (succeeded or failed-with-category).
  - :class:`VMTarget` — DB-agnostic VM identity passed to the engine.
  - :data:`PASS1_CATALOG_VERSION`, :func:`pass1_probe_names`.
  - :func:`run_collection_batch` — bounded-concurrency orchestrator.
  - :data:`FAILURE_CATEGORIES` — the canonical taxonomy strings.
"""

from app.core.collection.collector_spec import (
    PASS1_CATALOG_VERSION,
    pass1_probe_names,
    pass1_spec,
)
from app.core.collection.engine import (
    FAILURE_CATEGORIES,
    CollectionEngine,
    CollectionResult,
    VMTarget,
)
from app.core.collection.orchestrator import run_collection_batch

__all__ = [
    "CollectionEngine",
    "CollectionResult",
    "VMTarget",
    "FAILURE_CATEGORIES",
    "PASS1_CATALOG_VERSION",
    "pass1_probe_names",
    "pass1_spec",
    "run_collection_batch",
]
