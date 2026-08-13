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
from concurrent.futures import ThreadPoolExecutor
from typing import Union

from app.core.collection.engine import CollectionEngine, CollectionResult, VMTarget

logger = logging.getLogger(__name__)

# A callback may be sync or async; the orchestrator handles either.
ProgressCallback = Callable[[CollectionResult], Union[None, Awaitable[None]]]

# Failure categories that mean "couldn't even establish a usable session" —
# the circuit breaker counts only these, never a command-level / partial
# failure (those mean the host IS reachable, just quirky).
_CONNECTION_FAILURES = frozenset({"unreachable", "timeout", "auth_failed"})


async def run_collection_batch(
    *,
    targets: Iterable[VMTarget],
    engine: CollectionEngine,
    max_concurrency: int,
    max_concurrency_per_vcenter: int = 0,
    circuit_breaker_threshold: int = 0,
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
    :param max_concurrency_per_vcenter: Blast-radius cap — max simultaneous
        sessions to a single ``VMTarget.vcenter_id``. ``0`` disables the
        per-vCenter limit (legacy behavior).
    :param circuit_breaker_threshold: After this many CONSECUTIVE connection
        failures (unreachable/timeout/auth) to one vCenter, short-circuit its
        remaining targets to an ``unreachable`` result instead of connecting.
        ``0`` disables the breaker.
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

    # Per-vCenter concurrency limit (blast radius). Lazily created per vCenter
    # id; mirrors the bulk_capture pattern.
    per_vc_size = max(0, int(max_concurrency_per_vcenter))
    per_vc_semas: dict[int, asyncio.Semaphore] = {}

    def _vc_sema_for(vc_id: int | None) -> asyncio.Semaphore | None:
        if not per_vc_size or vc_id is None:
            return None
        if vc_id not in per_vc_semas:
            per_vc_semas[vc_id] = asyncio.Semaphore(per_vc_size)
        return per_vc_semas[vc_id]

    # Circuit breaker state: consecutive connection-failure streak per vCenter,
    # and the set of vCenters whose breaker has tripped. Mutated only under
    # results_lock (single-threaded asyncio, but the lock makes intent clear).
    breaker_threshold = max(0, int(circuit_breaker_threshold))
    vc_fail_streak: dict[int, int] = {}
    vc_tripped: set[int] = set()

    # Run blocking paramiko collections on a DEDICATED thread pool sized to
    # the concurrency ceiling. ``asyncio.to_thread`` would use the event
    # loop's *default* executor, whose worker count is
    # ``min(32, cpu_count + 4)`` — on a small pod (e.g. 4 cores → 8 threads)
    # that silently throttles true in-flight SSH below ``max_concurrency``,
    # so the operator's "10-20 simultaneous" target would never be met even
    # though the semaphore would admit them. A pool sized to the semaphore
    # guarantees the requested simultaneity is actually achievable and keeps
    # this workload off the shared default executor.
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=semaphore_size,
        thread_name_prefix="ssh-collect",
    )

    async def _collect_one(target: VMTarget) -> CollectionResult:
        """Run the blocking collect on the pool, honoring the per-vCenter
        limit. Never raises — returns a categorized result."""
        vc_sema = _vc_sema_for(target.vcenter_id)
        try:
            if vc_sema is not None:
                async with vc_sema:
                    return await loop.run_in_executor(executor, engine.collect, target)
            return await loop.run_in_executor(executor, engine.collect, target)
        except Exception as e:  # noqa: BLE001 — engine is supposed to be exception-safe
            # Belt-and-braces: if the engine ever raises (it shouldn't),
            # we still produce a result so the batch keeps moving.
            logger.error("collection.engine_raised vm_id=%s err=%s", target.vm_id, e)
            return CollectionResult(
                vm_id=target.vm_id,
                succeeded=False,
                failure_category="command_failed",
                failure_detail=f"engine raised: {type(e).__name__}: {e}",
            )

    async def _one(target: VMTarget) -> None:
        async with semaphore:
            vc = target.vcenter_id
            # Circuit breaker — if this vCenter has tripped, short-circuit
            # without connecting so we don't keep hammering a down or
            # mis-keyed environment.
            if breaker_threshold and vc is not None and vc in vc_tripped:
                result = CollectionResult(
                    vm_id=target.vm_id,
                    succeeded=False,
                    failure_category="unreachable",
                    failure_detail=(
                        f"Circuit breaker open for vCenter {vc} after "
                        f"{breaker_threshold} consecutive connection failures — "
                        "remaining targets short-circuited."
                    ),
                )
            else:
                result = await _collect_one(target)

            async with results_lock:
                results.append(result)
                # Update the per-vCenter failure streak / breaker.
                if breaker_threshold and vc is not None:
                    if result.failure_category in _CONNECTION_FAILURES:
                        vc_fail_streak[vc] = vc_fail_streak.get(vc, 0) + 1
                        if vc_fail_streak[vc] >= breaker_threshold and vc not in vc_tripped:
                            vc_tripped.add(vc)
                            logger.warning(
                                "collection.circuit_breaker_open vcenter=%s threshold=%s",
                                vc,
                                breaker_threshold,
                            )
                    elif result.succeeded:
                        # A clean collection resets the streak; a non-connection
                        # failure (command_failed/partial) neither trips nor
                        # resets — the host was reachable.
                        vc_fail_streak[vc] = 0

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

    try:
        await asyncio.gather(*(_one(t) for t in targets))
    finally:
        # Don't wait on in-flight threads here — gather already awaited every
        # _one, so all submitted work is done. shutdown() releases the pool.
        executor.shutdown(wait=False)
    return results
