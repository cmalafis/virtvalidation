"""Tests for the bounded-concurrency collection orchestrator.

Pins three guarantees:

  1. All targets reach a terminal state, regardless of per-VM outcome.
  2. In-flight concurrency NEVER exceeds the configured ceiling.
  3. A single VM raising an exception inside the engine (which shouldn't
     happen, but might) does not abort the batch.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from app.core.collection.engine import CollectionEngine, CollectionResult, VMTarget
from app.core.collection.orchestrator import run_collection_batch


def _result(vm_id: int, succeeded: bool = True, **kw) -> CollectionResult:
    return CollectionResult(
        vm_id=vm_id,
        succeeded=succeeded,
        completed_at=datetime.now(timezone.utc),
        **kw,
    )


class _CountingEngine:
    """A stand-in engine whose collect() takes a controlled amount of
    wall-clock to let the test observe the in-flight ceiling."""

    def __init__(self, *, in_flight_ceiling: int) -> None:
        self.ceiling = in_flight_ceiling
        self.max_seen = 0
        self.current = 0
        self.lock = threading.Lock()
        self.completed = 0

    def collect(self, target: VMTarget) -> CollectionResult:
        with self.lock:
            self.current += 1
            if self.current > self.max_seen:
                self.max_seen = self.current
        # Tiny sleep to let other workers pile up — without this, sequential
        # scheduling on a fast machine would never get above max_seen=1.
        import time

        time.sleep(0.02)
        with self.lock:
            self.current -= 1
            self.completed += 1
        return _result(target.vm_id)


@pytest.mark.asyncio
async def test_all_vms_reach_terminal_state():
    engine = _CountingEngine(in_flight_ceiling=10)
    targets = [VMTarget(vm_id=i, host=f"10.0.0.{i}") for i in range(1, 51)]
    results = await run_collection_batch(targets=targets, engine=engine, max_concurrency=10)
    assert len(results) == 50
    assert {r.vm_id for r in results} == set(range(1, 51))
    assert all(r.succeeded for r in results)


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_ceiling():
    engine = _CountingEngine(in_flight_ceiling=5)
    targets = [VMTarget(vm_id=i, host=f"10.0.0.{i}") for i in range(1, 101)]
    await run_collection_batch(targets=targets, engine=engine, max_concurrency=5)
    assert engine.max_seen <= 5, f"Concurrency ceiling violated: saw {engine.max_seen} in flight"
    # Sanity: with 100 VMs and 5 ceiling, we should have actually used some
    # parallelism — otherwise the test is meaningless.
    assert engine.max_seen >= 2


@pytest.mark.asyncio
async def test_per_vm_engine_exception_does_not_abort_batch():
    """The engine should never raise, but if it does the orchestrator must
    still produce a result for that VM and process the rest."""

    class _BadEngine:
        def __init__(self) -> None:
            self.calls = 0

        def collect(self, target: VMTarget) -> CollectionResult:
            self.calls += 1
            if target.vm_id == 3:
                raise RuntimeError("simulated engine bug")
            return _result(target.vm_id)

    engine = _BadEngine()
    targets = [VMTarget(vm_id=i, host=f"h{i}") for i in range(1, 6)]
    results = await run_collection_batch(targets=targets, engine=engine, max_concurrency=3)
    by_id = {r.vm_id: r for r in results}
    assert set(by_id) == {1, 2, 3, 4, 5}
    assert by_id[3].succeeded is False
    assert by_id[3].failure_category == "command_failed"
    assert "engine raised" in (by_id[3].failure_detail or "")
    assert all(r.succeeded for vmid, r in by_id.items() if vmid != 3)


@pytest.mark.asyncio
async def test_on_vm_complete_fires_once_per_vm():
    engine = _CountingEngine(in_flight_ceiling=4)
    targets = [VMTarget(vm_id=i, host=f"h{i}") for i in range(1, 21)]
    fired: list[int] = []
    fire_lock = asyncio.Lock()

    async def cb(result: CollectionResult) -> None:
        async with fire_lock:
            fired.append(result.vm_id)

    await run_collection_batch(
        targets=targets,
        engine=engine,
        max_concurrency=4,
        on_vm_complete=cb,
    )

    assert len(fired) == 20
    assert sorted(fired) == list(range(1, 21))


@pytest.mark.asyncio
async def test_callback_exception_does_not_abort_batch():
    """A buggy callback (e.g. DB failure during persist) must not stop
    the orchestrator — other VMs still need to complete."""
    engine = _CountingEngine(in_flight_ceiling=3)
    targets = [VMTarget(vm_id=i, host=f"h{i}") for i in range(1, 11)]
    seen: list[int] = []

    def cb(result: CollectionResult) -> None:
        if result.vm_id == 5:
            raise ValueError("simulated persist failure")
        seen.append(result.vm_id)

    results = await run_collection_batch(
        targets=targets, engine=engine, max_concurrency=3, on_vm_complete=cb
    )
    assert len(results) == 10
    # The callback succeeded for everything except vm 5; vm 5's result is
    # still in the results list, the callback failure was swallowed.
    assert 5 not in seen
    assert sorted(seen) == [i for i in range(1, 11) if i != 5]


@pytest.mark.asyncio
async def test_empty_target_list_returns_empty_list():
    engine = MagicMock(spec=CollectionEngine)
    results = await run_collection_batch(targets=[], engine=engine, max_concurrency=5)
    assert results == []
    engine.collect.assert_not_called()


@pytest.mark.asyncio
async def test_concurrency_clamped_to_safe_range():
    """max_concurrency=0 or negative should still produce a working batch
    (clamped to 1, sequential)."""
    engine = _CountingEngine(in_flight_ceiling=1)
    targets = [VMTarget(vm_id=i, host=f"h{i}") for i in range(1, 6)]
    results = await run_collection_batch(targets=targets, engine=engine, max_concurrency=0)
    assert len(results) == 5
    assert engine.max_seen == 1  # clamped to 1


@pytest.mark.asyncio
async def test_reaches_requested_simultaneity_of_20():
    """Operator requirement: validation must run on 10-20 machines at once.

    The other concurrency tests pin the *upper* bound (never exceed the
    ceiling). This one pins the *floor* deterministically: with a ceiling
    of 20 and >=20 targets, exactly 20 collections must be in flight
    simultaneously. A ``threading.Barrier(20)`` makes that provable —
    every worker blocks until 20 have arrived, so the batch can only
    progress if the orchestrator genuinely admits 20 at once. The barrier
    has a timeout so a regression (e.g. an accidental ceiling of 1) fails
    the test instead of hanging the suite.
    """

    class _BarrierEngine:
        def __init__(self, parties: int) -> None:
            self.barrier = threading.Barrier(parties, timeout=10)
            self.max_seen = 0
            self.current = 0
            self.lock = threading.Lock()

        def collect(self, target: VMTarget) -> CollectionResult:
            with self.lock:
                self.current += 1
                self.max_seen = max(self.max_seen, self.current)
            # Block until 20 workers are simultaneously in flight. This can
            # only succeed if the orchestrator admits 20 at once.
            self.barrier.wait()
            with self.lock:
                self.current -= 1
            return _result(target.vm_id)

    engine = _BarrierEngine(parties=20)
    targets = [VMTarget(vm_id=i, host=f"10.0.0.{i}") for i in range(1, 41)]
    results = await run_collection_batch(targets=targets, engine=engine, max_concurrency=20)
    assert len(results) == 40
    assert all(r.succeeded for r in results)
    # The barrier could only trip if 20 ran concurrently.
    assert engine.max_seen == 20, f"expected 20 simultaneous, saw {engine.max_seen}"


@pytest.mark.asyncio
async def test_circuit_breaker_short_circuits_after_consecutive_failures():
    """After N consecutive connection failures to one vCenter, the remaining
    targets in that vCenter are short-circuited without connecting."""

    class _AlwaysUnreachable:
        def __init__(self) -> None:
            self.calls = 0

        def collect(self, target: VMTarget) -> CollectionResult:
            self.calls += 1
            return _result(target.vm_id, succeeded=False, failure_category="unreachable")

    engine = _AlwaysUnreachable()
    # All same vCenter; serial (max_concurrency=1) so "consecutive" is exact.
    targets = [VMTarget(vm_id=i, host=f"h{i}", vcenter_id=1) for i in range(1, 11)]
    results = await run_collection_batch(
        targets=targets,
        engine=engine,
        max_concurrency=1,
        circuit_breaker_threshold=3,
    )
    assert len(results) == 10
    # Only the first 3 actually attempted a connection; the rest tripped.
    assert engine.calls == 3
    tripped = [r for r in results if "Circuit breaker" in (r.failure_detail or "")]
    assert len(tripped) == 7


@pytest.mark.asyncio
async def test_circuit_breaker_is_per_vcenter():
    """A tripped vCenter doesn't short-circuit a healthy one."""

    class _Engine:
        def collect(self, target: VMTarget) -> CollectionResult:
            if target.vcenter_id == 1:
                return _result(target.vm_id, succeeded=False, failure_category="unreachable")
            return _result(target.vm_id, succeeded=True)

    targets = [VMTarget(vm_id=i, host=f"h{i}", vcenter_id=1) for i in range(1, 6)] + [
        VMTarget(vm_id=i, host=f"h{i}", vcenter_id=2) for i in range(100, 105)
    ]
    results = await run_collection_batch(
        targets=targets,
        engine=_Engine(),
        max_concurrency=1,
        circuit_breaker_threshold=3,
    )
    by_id = {r.vm_id: r for r in results}
    # vCenter 2 all succeed regardless of vCenter 1's breaker.
    assert all(by_id[i].succeeded for i in range(100, 105))


@pytest.mark.asyncio
async def test_per_vcenter_concurrency_cap():
    """In-flight sessions to a single vCenter never exceed the per-vCenter cap
    even when the global ceiling is higher."""

    class _CountingPerVc:
        def __init__(self) -> None:
            self.current = 0
            self.max_seen = 0
            self.lock = threading.Lock()

        def collect(self, target: VMTarget) -> CollectionResult:
            import time

            with self.lock:
                self.current += 1
                self.max_seen = max(self.max_seen, self.current)
            time.sleep(0.02)
            with self.lock:
                self.current -= 1
            return _result(target.vm_id)

    engine = _CountingPerVc()
    targets = [VMTarget(vm_id=i, host=f"h{i}", vcenter_id=1) for i in range(1, 21)]
    await run_collection_batch(
        targets=targets,
        engine=engine,
        max_concurrency=20,
        max_concurrency_per_vcenter=3,
    )
    assert engine.max_seen <= 3, f"per-vCenter cap violated: saw {engine.max_seen}"


@pytest.mark.asyncio
async def test_scale_1000_mock_vms_complete():
    """Brief's scale requirement: 1000 VMs must complete in reasonable
    wall-clock at the default concurrency. Each mock takes ~20ms; 1000
    VMs at concurrency 25 should be ~800ms. Allow 10s for slow CI."""
    engine = _CountingEngine(in_flight_ceiling=25)
    targets = [VMTarget(vm_id=i, host=f"h{i}") for i in range(1, 1001)]
    import time

    start = time.monotonic()
    results = await run_collection_batch(targets=targets, engine=engine, max_concurrency=25)
    elapsed = time.monotonic() - start
    assert len(results) == 1000
    assert engine.completed == 1000
    assert engine.max_seen <= 25
    assert elapsed < 10.0, f"1000-VM batch took {elapsed:.2f}s — too slow"
