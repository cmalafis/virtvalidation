import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base, JSONType


class ValidationStatus(str, enum.Enum):
    passed = "pass"
    warn = "warn"
    failed = "fail"


class ValidationResult(Base):
    __tablename__ = "validation_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    vm_id: Mapped[int] = mapped_column(ForeignKey("vms.id", ondelete="CASCADE"), index=True)
    status: Mapped[ValidationStatus] = mapped_column(
        Enum(ValidationStatus, name="validation_status"), nullable=False
    )
    summary: Mapped[str] = mapped_column(String(2048), nullable=False, default="")
    findings: Mapped[list[dict]] = mapped_column(JSONType, nullable=False, default=list)
    remediation: Mapped[list[dict]] = mapped_column(JSONType, nullable=False, default=list)
    diff: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    validated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    vm = relationship("VM")

    __table_args__ = (Index("ix_validation_vm_validated_at", "vm_id", "validated_at"),)
