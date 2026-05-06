"""Pydantic schemas for migration plans + strategies.

Three layers:

  - :class:`PlanningStrategy*` — wizard payloads, persisted strategies.
  - :class:`PlanGenerate*` — async generation request/response shapes.
  - :class:`Plan*` / :class:`WaveRead` — what the API returns when fetching
    a stored plan. Same shape whether the plan was strategy-driven or
    legacy single-shot; new fields default to empty values for legacy rows.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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
# Plan generation (async)
# ---------------------------------------------------------------------------
class PlanScopeFilter(BaseModel):
    """Operator-supplied scope. At most one axis is honored — the API
    uses the most-specific axis present (vm_ids first, then
    source_vcenter_id, then environment / application_hint, then
    'all VMs')."""

    vm_ids: list[int] = Field(default_factory=list, max_length=10_000)
    source_vcenter_id: int | None = None
    environment: str | None = Field(default=None, max_length=64)
    application_hint: str | None = Field(default=None, max_length=128)


class PlanGenerateRequest(BaseModel):
    """Wizard submission. Either references a saved strategy or embeds
    a strategy inline (one-off generation without persisting the strategy).

    ``mapping_id`` ties the plan to a :class:`ResourceMapping` so MTV
    YAML export resolves source→target resources via real cluster names.
    Optional for back-compat — if absent, MTV YAML export falls back to
    per-VM ``target_*`` columns and surfaces a warning on the plan.
    """

    name: str = Field(min_length=1, max_length=255)
    strategy_id: int | None = None
    inline_strategy: PlanningStrategyCreate | None = None
    scope: PlanScopeFilter = Field(default_factory=PlanScopeFilter)
    mapping_id: int | None = None


class PlanGenerationTaskRead(BaseModel):
    """Status payload the wizard polls.

    Multi-stage hierarchical planner emits ``current_step`` values
    that map onto the stage names the chunker / chunked planner use.
    Optional fields (``chunks_total`` etc) populate during the chunked
    path; the legacy single-shot path leaves them ``None``.
    """

    task_id: str
    status: Literal["running", "completed", "failed"]
    current_step: Literal[
        "queued",
        "aggregating_data",
        "chunking",
        "planning_chunks",
        "planning_single_shot",
        "assembling",
        "reviewing",
        "llm_reasoning",
        "parsing_response",
        "validating",
        "persisting",
        "completed",
        "failed",
    ]
    progress_percent: int
    started_at: datetime
    completed_at: datetime | None = None
    plan_id: int | None = None
    error: str | None = None
    chunks_total: int | None = None
    chunks_complete: int | None = None
    current_chunk: str | None = None
    elapsed_seconds: int | None = None
    estimated_remaining_seconds: int | None = None
    path_taken: str | None = None


# ---------------------------------------------------------------------------
# Plan output
# ---------------------------------------------------------------------------
class WaveRead(BaseModel):
    wave_number: int
    name: str = ""
    vm_ids: list[int] = Field(default_factory=list)
    rationale: str = ""
    estimated_duration: str = ""
    estimated_risk: str = ""  # legacy field name kept for back-compat
    risk_level: str = ""
    considerations: str = ""
    applications_included: list[str] = Field(default_factory=list)
    applications_split_warning: str | None = None


class PlanChunkRead(BaseModel):
    """One chunk row from the hierarchical plan. Surfaced to the UI's
    chunk-navigation panel so operators see why each chunk exists."""

    model_config = ConfigDict(from_attributes=True)

    chunk_id: str
    sequence_index: int
    label: str
    reason_for_chunk: str
    partition_key: dict
    sub_key: dict
    hints: dict
    vm_ids: list[int]
    sequence_dependencies: list[str]
    chunk_rationale: str | None = None
    chunk_risk_level: str | None = None
    wave_numbers: list[int]


class PlanRead(BaseModel):
    """Strategy-driven plans populate every field; legacy plans leave
    rationale / warnings / next_actions empty. Waves are returned as
    dicts (not WaveRead) so the field set can grow without breaking
    existing consumers."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str = "Untitled plan"
    vm_ids: list[int]
    waves: list[dict]
    summary: str | None = None
    model: str
    strategy_id: int | None = None
    mapping_id: int | None = None
    plan_summary: str | None = None
    rationale: str | None = None
    warnings: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    supersedes_plan_id: int | None = None
    revision_number: int = 1
    created_at: datetime


# Legacy synchronous create payload — kept for back-compat with the
# existing POST /api/plans endpoint and its tests.
class PlanCreate(BaseModel):
    vm_ids: list[int] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Per-wave VM-move request
# ---------------------------------------------------------------------------
class WaveMoveVMRequest(BaseModel):
    vm_id: int
    target_wave_number: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1024)
