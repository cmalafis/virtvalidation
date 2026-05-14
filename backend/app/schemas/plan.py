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
    # ``mapping_ids`` is the canonical list of mappings consumed at
    # plan-generation time (one mapping per ``(vcenter, cluster)``
    # partition the plan covers). Coerce None → [] for legacy rows
    # whose mapping_ids column hasn't been backfilled.
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

    # Multi-cluster fan-out fields. POST /api/plans partitions the
    # selection by (vcenter, cluster, namespace) and creates one
    # MigrationPlan row per partition; ``plans`` lists every row
    # created in the fan-out, ``plan_count`` is len(plans). The
    # top-level PlanRead fields surface the first plan so single-
    # partition callers don't need to read ``plans[0]``.
    plans: list[dict] = Field(default_factory=list)
    plan_count: int = 0


# POST /api/plans payload — the canonical create path under the new
# pipeline.
class PlanCreate(BaseModel):
    vm_ids: list[int] = Field(min_length=1)
    # Operator-facing plan label. Falls back to "Untitled plan" when
    # not supplied so the legacy synchronous-create tests still pass.
    name: str = Field(default="Untitled plan", min_length=1, max_length=255)
    # Resource mappings that drive target namespace + network +
    # storage resolution. Each VM is routed via the per-pair-unique
    # ResourceMapping for its ``(source_vcenter_id, target_cluster_id)``
    # pair (see ``app.core.target_resolution``). Semantics:
    #   * field omitted (None)   → auto-resolve via target_resolution
    #   * empty list ``[]``      → operator explicitly opted out; only
    #     per-VM target_namespace_override covers Stage 0 validation
    #   * ``[id1, id2, ...]``    → use these mappings, route per-VM by
    #     resolved (vcenter, cluster)
    mapping_ids: list[int] | None = Field(default=None)
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


# ---------------------------------------------------------------------------
# POST /api/plans/preview — multi-cluster partition preview
# ---------------------------------------------------------------------------
class PreviewPartitionGroup(BaseModel):
    """One ``(vcenter, cluster, namespace)`` partition the operator's
    selection will fan out into. Each row corresponds to a Plan CR
    that POST /api/plans will create."""

    source_vcenter_id: int | None
    source_vcenter_name: str | None
    target_cluster_id: int | None
    target_cluster_name: str | None
    target_namespace: str | None
    vm_count: int
    estimated_waves: int
    mapping_id: int | None
    mapping_name: str | None
    network_targets: list[str] = Field(default_factory=list)
    storage_targets: list[str] = Field(default_factory=list)


class PreviewUnresolvedVM(BaseModel):
    vm_id: int
    vm_name: str
    reasons: list[str] = Field(default_factory=list)


class PlanPreviewResponse(BaseModel):
    total_vms: int
    resolvable: int
    unresolvable: int
    groups: list[PreviewPartitionGroup]
    unresolved: list[PreviewUnresolvedVM]


# ---------------------------------------------------------------------------
# POST /api/plans wrapper — returns the list of plans created by the
# multi-cluster fan-out. Single-partition selections produce a 1-entry
# list; multi-partition selections produce N entries.
# ---------------------------------------------------------------------------
class MultiPlanCreateResponse(BaseModel):
    plans: list[PlanRead]
