"""Schemas for manual plan edits — ``app.api.plan_edits``."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlanEditRequest(BaseModel):
    """One edit over many VMs. ``target_wave`` is an existing wave number
    or the literal ``"new"``; it is ignored for ``remove``."""

    operation: Literal["move_to_wave", "move_to_plan", "remove", "add"]
    vm_ids: list[int] = Field(min_length=1)
    target_wave: int | Literal["new"] | None = None
    target_plan_id: int | None = None
    # Evaluate only: return what would happen and which rules it breaks.
    dry_run: bool = False
    # Apply despite ``overridable`` violations. Audited.
    confirm_overrides: bool = False
    note: str | None = Field(default=None, max_length=500)


class EditViolation(BaseModel):
    code: str
    # blocking → refused; overridable → needs confirm_overrides; info → shown only.
    severity: str
    message: str
    plan_id: int | None = None
    wave_number: int | None = None
    family: str | None = None
    vm_ids: list[int] = Field(default_factory=list)
    vm_names: list[str] = Field(default_factory=list)
    count: int | None = None
    limit: int | None = None


class EditMove(BaseModel):
    vm_id: int
    vm_name: str
    from_plan_id: int | None = None
    from_wave: int | None = None
    to_plan_id: int | None = None
    to_wave: int | None = None


class EditPlanSummary(BaseModel):
    plan_id: int
    name: str
    vm_count: int
    wave_count: int
    touched_waves: list[int] = Field(default_factory=list)
    created_waves: list[int] = Field(default_factory=list)
    removed_waves: list[int] = Field(default_factory=list)


class PlanEditResponse(BaseModel):
    applied: bool
    dry_run: bool
    operation: str
    # True when the edit can only be applied with ``confirm_overrides``.
    requires_confirmation: bool
    violations: list[EditViolation] = Field(default_factory=list)
    moves: list[EditMove] = Field(default_factory=list)
    plans: list[EditPlanSummary] = Field(default_factory=list)
    # "queued" when the touched waves were handed to the LLM for a rationale.
    reannotation: str = "none"


class PlanPartition(BaseModel):
    source_vcenter_id: int | None = None
    source_vcenter_name: str | None = None
    target_cluster_id: int | None = None
    target_cluster_name: str | None = None
    target_namespace: str | None = None


class PlanVMRow(BaseModel):
    vm_id: int
    name: str
    wave_number: int
    environment: str | None = None
    ip_address: str | None = None
    os_family: str | None = None
    application_hint: str | None = None
    role: str | None = None
    # Name family, set only when the plan holds two or more of it.
    ha_family: str | None = None
    ha_family_size: int | None = None
    assessment_status: str | None = None
    lifecycle_state: str | None = None
    # In the wave's latest completed baseline run. None = no such run yet.
    baseline_covered: bool | None = None
    # The inventory row was deleted after the plan was generated.
    missing: bool = False


class PlanWaveBaseline(BaseModel):
    last_run_id: int | None = None
    last_completed_at: str | None = None
    missing_vm_ids: list[int] = Field(default_factory=list)


class PlanWaveState(BaseModel):
    wave_number: int
    vm_count: int
    baseline: PlanWaveBaseline = Field(default_factory=PlanWaveBaseline)
    # A baseline or validation run is pending/running on this wave.
    active_run: bool = False
    validated: bool = False


class PlanVMsResponse(BaseModel):
    plan_id: int
    status: str
    editable: bool
    partition: PlanPartition | None = None
    limits: dict[str, int] = Field(default_factory=dict)
    items: list[PlanVMRow] = Field(default_factory=list)
    total: int = 0
    waves: list[PlanWaveState] = Field(default_factory=list)


class EditTargetWave(BaseModel):
    wave_number: int
    vm_count: int
    estimated_risk: str | None = None


class EditTargetPlan(BaseModel):
    plan_id: int
    name: str
    same_series: bool = False
    series_index: int | None = None
    series_total: int | None = None
    migration_type: str = "cold"
    vm_count: int = 0
    waves: list[EditTargetWave] = Field(default_factory=list)


class IneligiblePlan(BaseModel):
    plan_id: int
    name: str
    reason: str


class EditTargetsResponse(BaseModel):
    partition: PlanPartition | None = None
    targets: list[EditTargetPlan] = Field(default_factory=list)
    ineligible: list[IneligiblePlan] = Field(default_factory=list)


class ReannotateAccepted(BaseModel):
    plan_id: int
    wave_number: int
    reannotation: str = "queued"
