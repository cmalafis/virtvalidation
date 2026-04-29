from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType


class AuditLog(Base):
    """Append-only audit trail row.

    No update/delete API endpoints exist by design — federal/regulated audit
    requirements call for an immutable trail. The middleware writes; readers
    only read.
    """

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        index=True,
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(255), default="system", nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
