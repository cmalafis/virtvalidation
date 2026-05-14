"""Bounded-concurrency orchestrator for the collection engine.

Drives :class:`CollectionEngine.collect` across many VMs at once with a
configurable concurrency ceiling (default 25, see
``settings.ssh_max_concurrency``). Uses ``asyncio.Semaphore`` to bound
in-flight work and ``asyncio.to_thread`` to run paramiko's blocking I/O
without blocking the event loop — same pattern as ``bulk_capture.py``.

Per-VM failures NEVER raise: the engine returns a result on failure, and
the orchestrator surfaces every result via the ``on_vm_complete`` callback.
A single misbehaving VM cannot abort the batch.

Progress is reported incrementally via ``on_vm_complete`` so the caller
(typically the BackgroundTask in :mod:`app.api.waves`) can update the
``BaselineRun.captured_vms`` / ``failed_vms`` counters in real time —
the UI polling sees progress tick instead of one big jump at the end.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Union

from app.core.collection.engine import CollectionEngine, CollectionResult, VMTarget

logger = logging.getLogger(__name__)

# A callback may be sync or async; the orchestrator handles either.
ProgressCallback = Callable[[CollectionResult], Union[None, Awaitable[None]]]


async def run_collection_batch(
    *,
    targets: Iterable[VMTarget],
    engine: CollectionEngine,
    max_concurrency: int,
    on_vm_complete: ProgressCallback | None = None,
) -> list[CollectionResult]:
    """Collect from every target in ``targets`` with bounded concurrency.

    Returns the full list of :class:`CollectionResult` once every VM has
    reached a terminal state. The callback fires per VM as soon as that
    VM completes (succeeded or failed), so callers can incrementally
    persist progress.

    :param max_concurrency: Hard upper bound on in-flight SSH sessions.
        Clamped to ``[1, 100]`` defensively even though the config validator
        also enforces this — defense in depth against a bad caller.
    """
    targets = list(targets)
    if not targets:
        return []

    # Clamp defensively. The config validator already restricts this
    # to [1, 100] but if a future caller bypasses settings we still want
    # safe behavior.
    semaphore_size = max(1, min(int(max_concurrency), 100))
    semaphore = asyncio.Semaphore(semaphore_size)
    results: list[CollectionResult] = []
    results_lock = asyncio.Lock()

    async def _one(target: VMTarget) -> None:
        async with semaphore:
            # Wrap the synchronous paramiko collect call in to_thread so it
            # doesn't block the event loop. The engine guarantees no
            # exception escapes a per-VM call.
            try:
                result = await asyncio.to_thread(engine.collect, target)
            except Exception as e:  # noqa: BLE001 — engine is supposed to be exception-safe
                # Belt-and-braces: if the engine ever raises (it shouldn't),
                # we still produce a result so the batch keeps moving.
                logger.error(
                    "collection.engine_raised vm_id=%s err=%s",
                    target.vm_id,
                    e,
                )
                result = CollectionResult(
                    vm_id=target.vm_id,
                    succeeded=False,
                    failure_category="command_failed",
                    failure_detail=f"engine raised: {type(e).__name__}: {e}",
                )

            async with results_lock:
                results.append(result)

            if on_vm_complete is not None:
                try:
                    maybe_coro = on_vm_complete(result)
                    if asyncio.iscoroutine(maybe_coro):
                        await maybe_coro
                except Exception as e:  # noqa: BLE001 — callback failures must not abort the batch
                    logger.error(
                        "collection.on_vm_complete_raised vm_id=%s err=%s",
                        result.vm_id,
                        e,
                    )

    await asyncio.gather(*(_one(t) for t in targets))
    return results
