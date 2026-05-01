"""network design review tables

Adds two tables that back the Network Design Review feature:

  - ``network_design_reviews`` — one row per review with notes, YAML,
    status, and the LLM's executive summary block.
  - ``network_findings`` — per-finding rows referencing a review with
    cascade delete; carry severity/confidence/triage so the UI can
    filter and the operator can mark findings as accepted or dismissed.

Revision ID: 0004_network_design_review
Revises: 0003_ssh_host_key_policy
Create Date: 2026-04-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004_network_design_review"
down_revision: str | Sequence[str] | None = "0003_ssh_host_key_policy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# JSON-with-postgres-variant — same shape used by the application models.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Postgres needs the enums created up front; SQLite stores them as
    # plain strings via the Enum column type's check-constraint.
    enums: list[sa.Enum] = []
    review_status = sa.Enum(
        "draft", "analyzing", "completed", "failed", name="network_review_status"
    )
    finding_category = sa.Enum(
        "coverage_gap",
        "config_mismatch",
        "missing_resource",
        "positive_confirmation",
        name="network_finding_category",
    )
    finding_severity = sa.Enum(
        "critical", "high", "medium", "low", "info", name="network_finding_severity"
    )
    finding_confidence = sa.Enum("high", "medium", "low", name="network_finding_confidence")
    finding_triage = sa.Enum("open", "accepted", "dismissed", name="network_finding_triage")
    enums.extend(
        [review_status, finding_category, finding_severity, finding_confidence, finding_triage]
    )
    if is_postgres:
        for e in enums:
            e.create(bind, checkfirst=True)

    op.create_table(
        "network_design_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            review_status,
            nullable=False,
            server_default="draft",
        ),
        sa.Column("customer_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("proposed_yaml", sa.Text(), nullable=False, server_default=""),
        sa.Column("analysis_results", JSONType, nullable=False, server_default=sa.text("'{}'")),
        sa.Column("last_analyzed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "network_findings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "review_id",
            sa.Integer(),
            sa.ForeignKey("network_design_reviews.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("category", finding_category, nullable=False),
        sa.Column("severity", finding_severity, nullable=False),
        sa.Column("confidence", finding_confidence, nullable=False, server_default="medium"),
        sa.Column("triage", finding_triage, nullable=False, server_default="open"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("proposed_evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("recommendation", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_table("network_findings")
    op.drop_table("network_design_reviews")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in (
            "network_finding_triage",
            "network_finding_confidence",
            "network_finding_severity",
            "network_finding_category",
            "network_review_status",
        ):
            sa.Enum(name=name).drop(bind, checkfirst=True)
