"""Pydantic schemas for migration plans + strategies.

Two layers:

  - :class:`PlanningStrategy*` — strategy catalog wizard payloads.
    Decoupled from plan generation in the rearchitecture; kept so the
    /strategies CRUD UI keeps working without touching plans.
  - :class:`Plan*` / :class:`WaveRead` — what the API returns when
    fetching a stored plan.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.plan import (
    ApplicationAtomicity,
    PrimaryGrouping,
    ProductionHandling,
    RiskApproach,
    WaveSizeTarget,
)


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
class PlanningStrategyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    primary_grouping: PrimaryGrouping = PrimaryGrouping.application
    wave_size_target: WaveSizeTarget = WaveSizeTarget.medium_10_20
    wave_size_custom: int | None = Field(default=None, ge=1, le=500)
    risk_approach: RiskApproach = RiskApproach.mixed
    production_handling: ProductionHandling = ProductionHandling.non_prod_first
    application_atomicity: ApplicationAtomicity = ApplicationAtomicity.all_together
    freeform_constraints: str = Field(default="", max_length=20_000)


class PlanningStrategyUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    primary_grouping: PrimaryGrouping | None = None
    wave_size_target: WaveSizeTarget | None = None
    wave_size_custom: int | None = Field(default=None, ge=1, le=500)
    risk_approach: RiskApproach | None = None
    production_handling: ProductionHandling | None = None
    application_atomicity: ApplicationAtomicity | None = None
    freeform_constraints: str | None = Field(default=None, max_length=20_000)


class PlanningStrategyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    name: str
    primary_grouping: PrimaryGrouping
    wave_size_target: WaveSizeTarget
    wave_size_custom: int | None = None
    risk_approach: RiskApproach
    production_handling: ProductionHandling
    application_atomicity: ApplicationAtomicity
    freeform_constraints: str
    created_by_actor: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------------
class WaveRead(BaseModel):
    wave_number: int
    name: str = ""
    vm_ids: list[int] = Field(default_factory=list)
    # Parallel-indexed to ``vm_ids`` — hostnames rendered in the wave
    # detail. Empty for legacy plans generated before the field was
    # added; the frontend falls back to ``/api/vms`` lookups.
    vm_names: list[str] = Field(default_factory=list)
    rationale: str = ""
    estimated_duration: str = ""
    estimated_risk: str = ""  # legacy field name kept for back-compat
    risk_level: str = ""
    considerations: str = ""
    applications_included: list[str] = Field(default_factory=list)
    applications_split_warning: str | None = None


class PlanRead(BaseModel):
    """API response shape for a stored plan.

    The new pipeline populates ``waves`` with AnnotatedWave.to_dict()
    output (description, risk_score, risk_rationale, notable_concerns,
    concurrency_group_id, method) plus the structural fields
    (vm_ids, group_ids, vm_count, estimated_risk). Surfaced as
    ``list[dict]`` so the field set can grow without breaking
    existing consumers.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str = "Untitled plan"
    vm_ids: list[int]
    waves: list[dict]
    summary: str | None = None
    model: str
    # ``mapping_ids`` is the canonical list; ``mapping_id`` is kept for
    # one release as a legacy alias = mapping_ids[0] if mapping_ids
    # else None. Both surfaced so existing UI bindings keep rendering
    # while the wizard switches over. Coerce None → [] for pre-multi-
    # mapping rows whose mapping_ids column hasn't been backfilled.
    mapping_id: int | None = None
    mapping_ids: list[int] = Field(default_factory=list)
    created_at: datetime

    @field_validator("mapping_ids", mode="before")
    @classmethod
    def _coerce_null_mapping_ids(cls, v):
        return v if v is not None else []

    # Async-lifecycle fields the rewritten POST /api/plans writes to
    # via its BackgroundTask. ``status`` is the source of truth for the
    # frontend's progress polling — pending → partitioning →
    # subpartitioning → splitting → packing → analyzing_concurrency →
    # annotating → emitting_yaml → complete → (operator) migrated, or
    # failed at any stage. ``error_message`` carries the verbatim typed-
    # exception text so the operator sees the same string in UI + logs.
    status: str = "complete"
    progress_message: str | None = None
    progress_percent: int = 100
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # Transient — only populated on the create response. GET /api/plans/{id}
    # leaves these empty since they aren't persisted on the model today;
    # the waves[] entries carry the group_ids list so groups can be re-
    # derived if a future feature needs them.
    groups: list[dict] = Field(default_factory=list)
    groups_formed: int = 0
    # Which path produced the wave assignment. ``llm`` = first attempt
    # succeeded. ``llm_retry_N`` = succeeded on retry N. ``mechanical_fallback``
    # = LLM failed every attempt; the topological-sort fallback produced
    # this plan. Helps operators debug LLM quality regressions.
    method: str = ""
    attempts: int = 0


# POST /api/plans payload — the canonical create path under the new
# pipeline.
class PlanCreate(BaseModel):
    vm_ids: list[int] = Field(min_length=1)
    # Operator-facing plan label. Falls back to "Untitled plan" when
    # not supplied so the legacy synchronous-create tests still pass.
    name: str = Field(default="Untitled plan", min_length=1, max_length=255)
    # Resource mappings that drive target namespace + network +
    # storage resolution. Each VM is routed to the mapping whose
    # ``vcenter_source_id`` matches the VM's source vCenter. Semantics:
    #   * field omitted (None)   → auto-resolve active mappings per
    #     vCenter touched by the selection (preserves CLI / pre-wizard
    #     behavior)
    #   * empty list ``[]``      → operator explicitly opted out; only
    #     per-VM target fields cover Stage 0 validation
    #   * ``[id1, id2, ...]``    → use these mappings, route per-VM by
    #     vcenter
    mapping_ids: list[int] | None = Field(default=None)
    # Singular legacy alias kept for one release so existing CLI /
    # test callers don't break. When provided ALONE (no mapping_ids),
    # treated as ``mapping_ids=[mapping_id]``. Ignored when
    # ``mapping_ids`` is also set.
    mapping_id: int | None = Field(default=None)
    # Retained for back-compat with the synchronous tests; the new
    # pipeline ignores them. The mechanical pre-classifier always
    # runs, and HA spreading is enforced by the family-aware Stage 3
    # split rather than a per-call toggle.
    preclassification_enabled: bool = True
    ha_strategy: str = Field(default="spread", pattern=r"^(spread|together|auto)$")


class PreviewGroupsResponse(BaseModel):
    """Result of POST /api/plans/preview-groups — no plan persisted.

    Lets the operator preview how the pre-classifier WOULD group their
    VMs before spending LLM time on wave assignment. Useful for
    debugging ("why is VM X in the same group as Y?") and for
    showing the value of mechanical grouping during demos.
    """

    vm_count: int
    groups_formed: int
    groups: list[dict]
    over_ceiling: bool
    ceiling: int
