"""Operator-defined target StorageClass entities on an OCP cluster.

Companion to :mod:`app.models.target_network`. The mapping editor's
storage dropdowns are populated from these rows. Live discovery
(``oc get sc``) is intentionally not the source of truth — federal
air-gapped clusters can't be probed live, so the operator declares
what's available.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class StorageAccessMode(str, enum.Enum):
    rwo = "ReadWriteOnce"
    rwx = "ReadWriteMany"
    rom = "ReadOnlyMany"


class TargetStorageClass(Base):
    __tablename__ = "target_storage_classes"

    id: Mapped[int] = mapped_column(primary_key=True)
    ocp_target_id: Mapped[int] = mapped_column(
        ForeignKey("ocp_targets.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # values_callable: send Kubernetes access-mode strings ("ReadWriteOnce",
    # …) — the .value side of the enum, matching what the Alembic migration
    # used to CREATE TYPE. Without this SQLAlchemy would send the member
    # NAMES ("rwo"/"rwx"/"rom") which Postgres rejects with
    # ``invalid input value for enum target_sc_access_mode``. See the
    # SQLAlchemy enum rule in CLAUDE.md.
    access_mode: Mapped[StorageAccessMode] = mapped_column(
        Enum(
            StorageAccessMode,
            name="target_sc_access_mode",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
        default=StorageAccessMode.rwo,
        server_default=StorageAccessMode.rwo.value,
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint("ocp_target_id", "name", name="uq_target_sc_per_cluster"),
        Index("ix_target_sc_ocp_target", "ocp_target_id"),
    )
