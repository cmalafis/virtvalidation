"""Migration planning data model.

Two related tables:

  - :class:`PlanningStrategy` — captures customer intent through
    structured wizard choices (primary grouping, wave sizing, risk
    approach, etc.) plus a freeform-constraints field. The strategy
    drives prompt construction and is referenced by every plan it
    produces.

  - :class:`MigrationPlan` — the LLM-generated plan itself. Stores
    the rendered prompt and raw response alongside the parsed waves
    so federal reviewers can audit the chain of reasoning. Plans
    chain through ``supersedes_plan_id`` to support per-wave
    refinements as new revisions.
"""

import enum
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType


# ---------------------------------------------------------------------------
# Enums — every wizard choice is a controlled vocabulary so the prompt
# builder doesn't have to handle free strings, and federal audit can
# correlate plans across runs.
# ---------------------------------------------------------------------------
class PrimaryGrouping(str, enum.Enum):
    application = "application"
    vcenter_folder = "vcenter_folder"
    application_owner = "application_owner"
    environment = "environment"
    business_unit = "business_unit"
    data_classification = "data_classification"
    llm_decides = "llm_decides"


class WaveSizeTarget(str, enum.Enum):
    small_5_10 = "small_5_10"  # slow but safe
    medium_10_20 = "medium_10_20"  # balanced — wizard default
    large_20_50 = "large_20_50"  # aggressive
    custom = "custom"


class RiskApproach(str, enum.Enum):
    low_first = "low_first"  # build confidence
    high_first = "high_first"  # tackle complex first
    mixed = "mixed"  # balance per wave — wizard default


class ProductionHandling(str, enum.Enum):
    mixed = "mixed"
    non_prod_first = "non_prod_first"  # wizard default
    prod_dedicated_waves = "prod_dedicated_waves"


class ApplicationAtomicity(str, enum.Enum):
    all_together = "all_together"
    can_split = "can_split"
    per_app_choice = "per_app_choice"


class PlanningStrategy(Base):
    """Customer intent captured by the planning wizard.

    A strategy is reusable — operators commonly run the same
    "DHA-quarterly" strategy through several plan revisions as
    inventory shifts. Each plan references the strategy that drove
    its generation so federal reviewers can audit the choice.
    """

    __tablename__ = "planning_strategies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    primary_grouping: Mapped[PrimaryGrouping] = mapped_column(
        Enum(PrimaryGrouping, name="primary_grouping"),
        default=PrimaryGrouping.application,
        server_default=PrimaryGrouping.application.value,
        nullable=False,
    )
    wave_size_target: Mapped[WaveSizeTarget] = mapped_column(
        Enum(WaveSizeTarget, name="wave_size_target"),
        default=WaveSizeTarget.medium_10_20,
        server_default=WaveSizeTarget.medium_10_20.value,
        nullable=False,
    )
    # Only consulted when wave_size_target == custom. The wizard
    # surfaces this as a single number input; we don't enforce a
    # max in the model — operators with edge cases can set extreme
    # values and own the consequences.
    wave_size_custom: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_approach: Mapped[RiskApproach] = mapped_column(
        Enum(RiskApproach, name="risk_approach"),
        default=RiskApproach.mixed,
        server_default=RiskApproach.mixed.value,
        nullable=False,
    )
    production_handling: Mapped[ProductionHandling] = mapped_column(
        Enum(ProductionHandling, name="production_handling"),
        default=ProductionHandling.non_prod_first,
        server_default=ProductionHandling.non_prod_first.value,
        nullable=False,
    )
    application_atomicity: Mapped[ApplicationAtomicity] = mapped_column(
        Enum(ApplicationAtomicity, name="application_atomicity"),
        default=ApplicationAtomicity.all_together,
        server_default=ApplicationAtomicity.all_together.value,
        nullable=False,
    )
    # Free-form text the LLM interprets. Examples in the wizard:
    # "No migrations during March", "These two applications must NOT
    # migrate in the same wave", "Database tier needs 2-week soak".
    # The prompt builder injects this verbatim — we trust the LLM to
    # disambiguate, and surface anything it can't via plan warnings.
    freeform_constraints: Mapped[str] = mapped_column(
        Text, default="", server_default="", nullable=False
    )
    # Audit field — who authored this strategy. Pulled from the
    # x-actor header on the create request, defaults to "user".
    created_by_actor: Mapped[str] = mapped_column(
        String(255), default="user", server_default="user", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MigrationPlan(Base):
    __tablename__ = "migration_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Plain-language label for the plan. Defaults to "Untitled plan" on
    # legacy rows; the wizard always sets a name.
    name: Mapped[str] = mapped_column(
        String(255), default="Untitled plan", server_default="Untitled plan", nullable=False
    )
    vm_ids: Mapped[list[int]] = mapped_column(JSONType, nullable=False)
    waves: Mapped[list[dict]] = mapped_column(JSONType, nullable=False)
    # Legacy 1-line summary — kept for back-compat with the old planner
    # output. New strategy-driven plans populate plan_summary + rationale
    # + next_actions instead and leave summary mirroring plan_summary.
    summary: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False)

    # ---- strategy-driven planning fields (nullable for legacy plans) ----
    strategy_id: Mapped[int | None] = mapped_column(
        ForeignKey("planning_strategies.id", ondelete="SET NULL"), nullable=True
    )
    # The full prompt sent to the LLM. Stored verbatim so federal
    # reviewers can reproduce the call if they need to.
    generation_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Raw LLM response body (pre-parse). Useful for debugging when
    # parsing fails — operators can re-run the parser without
    # re-billing the LLM.
    generation_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Plan-level fields the strategy planner produces alongside waves.
    plan_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    # LLM-flagged concerns about the plan itself — usually conflicts
    # between customer constraints and inventory shape (e.g., "you said
    # max 10 VMs/wave but app X has 23 indivisible VMs").
    warnings: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    next_actions: Mapped[list[str]] = mapped_column(
        JSONType, default=list, nullable=False
    )

    # Plan revisioning. supersedes_plan_id chains backwards to the
    # previous revision; revision_number increments. The chain lets
    # the comparison view walk history without a self-join hack.
    supersedes_plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("migration_plans.id", ondelete="SET NULL"), nullable=True
    )
    revision_number: Mapped[int] = mapped_column(
        Integer, default=1, server_default="1", nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    strategy: Mapped["PlanningStrategy | None"] = relationship(
        "PlanningStrategy", foreign_keys=[strategy_id]
    )
