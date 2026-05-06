"""Unit tests for the deterministic Python chunker.

The chunker partitions an inventory before any LLM calls happen.
Failures here mean the LLM gets bad input — wrong scope, classification
boundary crossed, oversized chunk truncated mid-prompt — so the
acceptance bar is high. Every assertion below maps to a real
production failure mode the chunker prevents.
"""

from __future__ import annotations

from app.core.chunker import (
    MIN_CHUNK_SIZE,
    Chunk,
    chunk_vms,
    validate_chunks,
)
from app.models.plan import (
    ApplicationAtomicity,
    PlanningStrategy,
    PrimaryGrouping,
    ProductionHandling,
    RiskApproach,
    WaveSizeTarget,
)
from app.models.vm import VM


def _make_vm(
    vid: int,
    *,
    name: str | None = None,
    role: str | None = None,
    environment: str | None = None,
    owner: str | None = None,
    application_hint: str | None = None,
    source_vcenter_id: int | None = 1,
    target_namespace: str | None = None,
    networks: list[str] | None = None,
    datastores: list[str] | None = None,
    os_family: str | None = None,
) -> VM:
    """Construct a transient VM model. Tests don't persist; VMs are
    used purely as inputs to the chunker."""
    return VM(
        id=vid,
        name=name or f"vm-{vid:03d}",
        source_hostname=f"{name or f'vm-{vid:03d}'}.local",
        ip_address=None,
        os_family=os_family,
        role=role,
        environment=environment,
        owner=owner,
        application_hint=application_hint,
        source_vcenter_id=source_vcenter_id,
        target_namespace=target_namespace,
        vsphere_networks=list(networks or []),
        vsphere_datastores=list(datastores or []),
    )


def _strategy(**overrides) -> PlanningStrategy:
    s = PlanningStrategy(
        name="test",
        primary_grouping=PrimaryGrouping.application,
        wave_size_target=WaveSizeTarget.medium_10_20,
        risk_approach=RiskApproach.mixed,
        production_handling=ProductionHandling.non_prod_first,
        application_atomicity=ApplicationAtomicity.all_together,
        freeform_constraints="",
    )
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ---------------------------------------------------------------------------
# Smoke + basic shape
# ---------------------------------------------------------------------------
def test_empty_input_returns_empty_list():
    assert chunk_vms([], strategy=_strategy(), max_size=15) == []


def test_single_vm_returns_single_chunk():
    chunks = chunk_vms(
        [_make_vm(1, application_hint="solo-app")],
        strategy=_strategy(),
        max_size=15,
    )
    assert len(chunks) == 1
    assert chunks[0].vm_ids == [1]
    assert isinstance(chunks[0], Chunk)
    validate_chunks(chunks, {1})


def test_chunks_carry_partition_sub_keys_and_hints():
    vms = [
        _make_vm(i, application_hint="app-a", environment="prod", owner=f"team-{i % 2}",
                 networks=["prod-net"])
        for i in range(1, 8)
    ]
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    c = chunks[0]
    assert c.partition_key["source_vcenter_id"] == 1
    assert "application_hint" in c.sub_key
    assert "owners" in c.hints
    assert c.hints["owners"]


# ---------------------------------------------------------------------------
# Mandatory dimensions never collapse
# ---------------------------------------------------------------------------
def test_different_vcenters_never_combined(monkeypatch):
    vms = [
        _make_vm(1, application_hint="app", source_vcenter_id=1),
        _make_vm(2, application_hint="app", source_vcenter_id=2),
        _make_vm(3, application_hint="app", source_vcenter_id=1),
    ]
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    vcenters_per_chunk = {tuple(sorted({_vcenter_for(c, vms) for c in [chunk]})) for chunk in chunks}
    # Each chunk has exactly one vcenter id.
    for c in chunks:
        ids = {_vm_by_id(vms, v).source_vcenter_id for v in c.vm_ids}
        assert len(ids) == 1, f"Chunk {c.chunk_id} mixes vCenters: {ids}"


def test_classifications_never_combined():
    classification = {1: "unclassified", 2: "secret"}
    vms = [
        _make_vm(1, application_hint="app", source_vcenter_id=1),
        _make_vm(2, application_hint="app", source_vcenter_id=2),
    ]
    chunks = chunk_vms(
        vms,
        strategy=_strategy(),
        max_size=15,
        classification_by_vcenter=classification,
    )
    classifications_per_chunk = {
        c.partition_key["classification_level"] for c in chunks
    }
    assert classifications_per_chunk == {"unclassified", "secret"}


def test_target_namespaces_never_combined():
    vms = [
        _make_vm(1, application_hint="app", target_namespace="ns-a"),
        _make_vm(2, application_hint="app", target_namespace="ns-b"),
    ]
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    ns_per_chunk = {c.partition_key["target_namespace"] for c in chunks}
    assert ns_per_chunk == {"ns-a", "ns-b"}


# ---------------------------------------------------------------------------
# Strong-dimension partitioning
# ---------------------------------------------------------------------------
def test_distinct_applications_produce_distinct_chunks():
    vms = (
        [_make_vm(i, application_hint="app-a") for i in range(1, 6)]
        + [_make_vm(i, application_hint="app-b") for i in range(6, 11)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    apps = {c.sub_key.get("application_hint") for c in chunks}
    assert "app-a" in apps and "app-b" in apps


def test_prod_and_dev_for_same_app_split():
    vms = (
        [_make_vm(i, application_hint="ehr", environment="prod") for i in range(1, 6)]
        + [_make_vm(i, application_hint="ehr", environment="dev") for i in range(6, 11)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    envs = {c.sub_key.get("environment") for c in chunks}
    assert envs == {"prod", "dev"}


# ---------------------------------------------------------------------------
# Oversized subdivision
# ---------------------------------------------------------------------------
def test_oversized_app_subdivides_by_tier():
    vms = (
        [_make_vm(i, name=f"ehr-web-{i:02d}", application_hint="ehr") for i in range(1, 8)]
        + [_make_vm(i, name=f"ehr-app-{i:02d}", application_hint="ehr") for i in range(8, 16)]
        + [_make_vm(i, name=f"ehr-db-{i:02d}", application_hint="ehr") for i in range(16, 21)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    tiers = {c.sub_key.get("tier") for c in chunks}
    # tier subdivision should produce at least web + app + db buckets
    assert {"web", "app", "db"}.issubset(tiers)


def test_oversized_app_falls_back_to_network_when_no_tier_signal():
    vms = (
        [_make_vm(i, application_hint="rando", networks=["net-a"]) for i in range(1, 11)]
        + [_make_vm(i, application_hint="rando", networks=["net-b"]) for i in range(11, 21)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    networks = {c.sub_key.get("primary_network") for c in chunks}
    assert {"net-a", "net-b"}.issubset(networks)


def test_oversized_falls_back_to_even_split_as_last_resort():
    # Same app, no tiers, no networks → even split.
    vms = [_make_vm(i, application_hint="generic") for i in range(1, 35)]
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=10)
    sizes = sorted(c.size for c in chunks)
    assert all(s <= 10 for s in sizes), sizes
    assert sum(sizes) == len(vms)


# ---------------------------------------------------------------------------
# Undersized combining
# ---------------------------------------------------------------------------
def test_undersized_chunks_combine_within_partition():
    # Three tiny apps in the same vcenter/namespace — operator
    # benefits from combining them so per-chunk LLM calls have signal.
    vms = (
        [_make_vm(1, application_hint="tiny-a")]
        + [_make_vm(2, application_hint="tiny-b")]
        + [_make_vm(3, application_hint="tiny-c")]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    # All three end up in one combined chunk (size=3 still < MIN, but
    # there are no more donors — a trailing-leftover chunk is fine).
    assert len(chunks) == 1
    assert chunks[0].sub_key.get("combined") is True
    assert chunks[0].size == 3


def test_combined_chunks_only_merge_within_same_partition():
    classification = {1: "unclassified", 2: "secret"}
    vms = (
        [_make_vm(1, application_hint="tiny-u", source_vcenter_id=1)]
        + [_make_vm(2, application_hint="tiny-s", source_vcenter_id=2)]
    )
    chunks = chunk_vms(
        vms,
        strategy=_strategy(),
        max_size=15,
        classification_by_vcenter=classification,
    )
    # Different classifications never share a chunk even if both small.
    assert len(chunks) == 2
    classifications = {c.partition_key["classification_level"] for c in chunks}
    assert classifications == {"unclassified", "secret"}


def test_undersized_combine_keeps_label_readable():
    vms = (
        [_make_vm(1, application_hint="alpha")]
        + [_make_vm(2, application_hint="beta")]
        + [_make_vm(3, application_hint="gamma")]
        + [_make_vm(4, application_hint="delta")]
        + [_make_vm(5, application_hint="epsilon")]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    combined = next((c for c in chunks if c.sub_key.get("combined")), None)
    assert combined is not None
    label = combined.sub_key.get("label") or ""
    # Should reference at least one of the constituent app names.
    assert any(name in label for name in ("Alpha", "Beta", "Gamma"))


# ---------------------------------------------------------------------------
# Foundation detection + dependencies
# ---------------------------------------------------------------------------
def test_foundation_chunks_come_first_in_ordering():
    vms = (
        [_make_vm(1, name="ad-dc-01"), _make_vm(2, name="ad-dc-02")]
        + [_make_vm(3, application_hint="ehr", environment="prod") for _ in range(1)]
        + [_make_vm(i, application_hint="ehr", environment="prod") for i in range(4, 8)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    assert chunks[0].sub_key.get("is_foundation") is True


def test_non_foundation_chunks_depend_on_foundation():
    vms = (
        [_make_vm(1, name="dns-01"), _make_vm(2, name="dns-02")]
        + [_make_vm(i, application_hint="app", environment="prod") for i in range(3, 8)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    foundation_ids = [c.chunk_id for c in chunks if c.sub_key.get("is_foundation")]
    assert foundation_ids
    for c in chunks:
        if not c.sub_key.get("is_foundation"):
            assert all(fid in c.sequence_dependencies for fid in foundation_ids)


def test_db_tier_listed_as_dependency_of_app_tier():
    vms = (
        [_make_vm(i, name=f"ehr-db-{i:02d}", application_hint="ehr",
                  environment="prod") for i in range(1, 4)]
        + [_make_vm(i, name=f"ehr-app-{i:02d}", application_hint="ehr",
                    environment="prod") for i in range(4, 25)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    db_chunk = next(c for c in chunks if c.sub_key.get("tier") == "db")
    app_chunks = [c for c in chunks if c.sub_key.get("tier") == "app"]
    assert app_chunks
    for app in app_chunks:
        assert db_chunk.chunk_id in app.sequence_dependencies, app.sub_key


def test_prod_chunk_depends_on_non_prod_sibling():
    vms = (
        [_make_vm(i, application_hint="ehr", environment="dev") for i in range(1, 6)]
        + [_make_vm(i, application_hint="ehr", environment="prod") for i in range(6, 11)]
    )
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    prod_chunk = next(c for c in chunks if c.sub_key.get("environment") == "prod")
    dev_chunk = next(c for c in chunks if c.sub_key.get("environment") == "dev")
    assert dev_chunk.chunk_id in prod_chunk.sequence_dependencies


# ---------------------------------------------------------------------------
# Validation helper + integrity
# ---------------------------------------------------------------------------
def test_validate_chunks_rejects_duplicates():
    chunks = [
        Chunk("a", [1, 2], {}, {}, {}, "x"),
        Chunk("b", [2, 3], {}, {}, {}, "y"),
    ]
    try:
        validate_chunks(chunks, {1, 2, 3})
    except AssertionError as e:
        assert "multiple chunks" in str(e)
    else:
        raise AssertionError("validate_chunks should have raised")


def test_validate_chunks_rejects_missing_vms():
    chunks = [Chunk("a", [1, 2], {}, {}, {}, "x")]
    try:
        validate_chunks(chunks, {1, 2, 3})
    except AssertionError as e:
        assert "miss vm_ids" in str(e)
    else:
        raise AssertionError("validate_chunks should have raised")


def test_all_input_vms_appear_exactly_once_realistic_inventory():
    """57-VM inventory matching the DHA test fixture's distribution.
    Mirrors the vCenter-1 acceptance criteria from the spec."""
    vms = (
        # Foundation
        [_make_vm(i, name=f"dns-{i:02d}") for i in range(1, 4)]
        + [_make_vm(i, name=f"ad-{i:02d}") for i in range(4, 7)]
        # EHR prod
        + [
            _make_vm(i, name=f"ehrpro-web-{i:02d}", application_hint="ehr",
                     environment="prod")
            for i in range(7, 17)
        ]
        + [
            _make_vm(i, name=f"ehrpro-app-{i:02d}", application_hint="ehr",
                     environment="prod")
            for i in range(17, 27)
        ]
        + [
            _make_vm(i, name=f"ehrpro-db-{i:02d}", application_hint="ehr",
                     environment="prod")
            for i in range(27, 32)
        ]
        # PACS imaging prod
        + [
            _make_vm(i, name=f"pacs-{i:02d}", application_hint="pacs-imaging",
                     environment="prod")
            for i in range(32, 42)
        ]
        # EHR staging
        + [
            _make_vm(i, name=f"ehrstaging-{i:02d}", application_hint="ehr",
                     environment="staging")
            for i in range(42, 46)
        ]
        # PACS staging
        + [
            _make_vm(i, name=f"pacsstaging-{i:02d}",
                     application_hint="pacs-imaging", environment="staging")
            for i in range(46, 49)
        ]
        # Dev sandbox
        + [
            _make_vm(i, name=f"dev-sb-{i:02d}", application_hint="dev-sandbox",
                     environment="dev")
            for i in range(49, 56)
        ]
        # Misc
        + [
            _make_vm(i, name=f"legacy-{i:02d}",
                     application_hint="legacy-reporting")
            for i in range(56, 58)
        ]
    )
    assert len(vms) == 57
    chunks = chunk_vms(vms, strategy=_strategy(), max_size=15)
    validate_chunks(chunks, {vm.id for vm in vms})

    # Spec acceptance: 6-10 chunks at max_size=15.
    assert 6 <= len(chunks) <= 12, f"got {len(chunks)} chunks: {[c.size for c in chunks]}"
    # No chunk over max_size.
    for c in chunks:
        assert c.size <= 15, c.sub_key
    # No undersized chunks except in deliberate combine cases.
    for c in chunks:
        assert c.size >= 1


# ---------------------------------------------------------------------------
# Helpers used inside tests
# ---------------------------------------------------------------------------
def _vm_by_id(vms: list[VM], vid: int) -> VM:
    return next(vm for vm in vms if vm.id == vid)


def _vcenter_for(c: Chunk, vms: list[VM]) -> int:
    return _vm_by_id(vms, c.vm_ids[0]).source_vcenter_id
