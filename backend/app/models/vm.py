import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class VMStatus(str, enum.Enum):
    discovered = "discovered"
    baseline_captured = "baseline_captured"
    migrated = "migrated"
    validated = "validated"
    failed = "failed"


class VM(Base):
    __tablename__ = "vms"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    source_hostname: Mapped[str] = mapped_column(String(255))
    target_hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    os_family: Mapped[str | None] = mapped_column(String(32), nullable=True)
    role: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[VMStatus] = mapped_column(
        Enum(VMStatus, name="vm_status"), default=VMStatus.discovered, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    snapshots: Mapped[list["BaselineSnapshot"]] = relationship(
        back_populates="vm",
        cascade="all, delete-orphan",
        order_by="BaselineSnapshot.captured_at.desc()",
    )


class BaselineSnapshot(Base):
    __tablename__ = "baseline_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    vm_id: Mapped[int] = mapped_column(
        ForeignKey("vms.id", ondelete="CASCADE"), index=True
    )
    ssh_user: Mapped[str] = mapped_column(String(64))
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    vm: Mapped[VM] = relationship(back_populates="snapshots")

    __table_args__ = (Index("ix_snapshots_vm_captured_at", "vm_id", "captured_at"),)
