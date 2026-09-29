"""``app.core.plan_chunking`` — pure-Python batching + series naming."""

from __future__ import annotations

import random
from types import SimpleNamespace

from app.core.plan_chunking import chunk_partition, compose_series_name, slug_for_plan_name


def _vm(i: int, name: str, hint: str | None = None):
    return SimpleNamespace(id=i, name=name, application_hint=hint)


def test_fits_in_one_batch_returns_original_order():
    vms = [_vm(3, "c"), _vm(1, "a"), _vm(2, "b")]
    assert chunk_partition(vms, 3) == [vms]
    assert chunk_partition(vms, 10) == [vms]


def test_balanced_sizes_never_leave_a_tiny_tail():
    vms = [_vm(i, f"vm-{i:04d}") for i in range(3007)]
    sizes = [len(c) for c in chunk_partition(vms, 1000)]
    # Four batches (the 1000 cap is hard), balanced rather than 1000/1000/1000/7.
    assert sizes == [752, 752, 752, 751]
    assert sum(sizes) == 3007 and max(sizes) <= 1000


def test_seven_into_three_is_3_2_2():
    vms = [_vm(i, f"vm-{i}") for i in range(7)]
    assert [len(c) for c in chunk_partition(vms, 3)] == [3, 2, 2]


def test_deterministic_regardless_of_input_order():
    vms = [_vm(i, f"app-{i % 7}-db-{i:03d}", hint=f"app{i % 5}") for i in range(50)]
    shuffled = list(vms)
    random.Random(7).shuffle(shuffled)
    a = [[v.id for v in c] for c in chunk_partition(vms, 8)]
    b = [[v.id for v in c] for c in chunk_partition(shuffled, 8)]
    assert a == b


def test_related_vms_stay_adjacent():
    # Two apps, two HA families each. With a batch size of 4 every batch
    # should hold one app's VMs, not a mix.
    vms = [
        _vm(1, "billing-db-01", "billing"),
        _vm(2, "crm-web-02", "crm"),
        _vm(3, "billing-web-01", "billing"),
        _vm(4, "crm-db-01", "crm"),
        _vm(5, "billing-db-02", "billing"),
        _vm(6, "crm-web-01", "crm"),
        _vm(7, "billing-web-02", "billing"),
        _vm(8, "crm-db-02", "crm"),
    ]
    chunks = chunk_partition(vms, 4)
    assert [{v.application_hint for v in c} for c in chunks] == [{"billing"}, {"crm"}]
    # And inside a batch, an HA family's members are contiguous.
    assert [v.name for v in chunks[0]] == [
        "billing-db-01",
        "billing-db-02",
        "billing-web-01",
        "billing-web-02",
    ]


def test_every_vm_lands_in_exactly_one_batch():
    vms = [_vm(i, f"vm-{i}") for i in range(101)]
    seen = [v.id for c in chunk_partition(vms, 10) for v in c]
    assert sorted(seen) == list(range(101)) and len(seen) == 101


def test_single_plan_keeps_name_verbatim():
    assert compose_series_name("Q4 Wave", index=1, total=1) == "Q4 Wave"


def test_series_appends_number():
    assert compose_series_name("Q4 Wave", index=2, total=3) == "q4-wave-2"


def test_multi_partition_keeps_partition_slug_in_the_middle():
    name = compose_series_name(
        "Q4 Wave",
        vcenter_name="vc01",
        cluster_name="ocp-east",
        namespace="prod",
        index=3,
        total=5,
        multi_partition=True,
    )
    assert name == "q4-wave-vc01-ocp-east-prod-3"


def test_multi_partition_single_batch_has_no_number():
    name = compose_series_name(
        "Q4", vcenter_name="vc01", namespace="prod", index=1, total=1, multi_partition=True
    )
    assert name == "q4-vc01-prod"


def test_number_survives_rfc1123_truncation():
    long_base = "x" * 100
    name = compose_series_name(long_base, index=12, total=20)
    assert len(name) <= 63
    assert name.endswith("-12")


def test_slug_never_ends_with_dash_after_truncation():
    assert slug_for_plan_name("abc-def", max_len=4) == "abc"
    assert slug_for_plan_name("!!!") == "plan"
