import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SchedulePreset(str, enum.Enum):
    twice_daily = "twice_daily"  # 06:00 and 18:00 UTC
    once_daily = "once_daily"  # 06:00 UTC
    hourly = "hourly"  # every hour at :00


class AppSettings(Base):
    """Singleton settings row — always id=1."""

    __tablename__ = "app_settings"

    id: Mapped[int] = mapped_column(primary_key=True)
    ollama_model: Mapped[str] = mapped_column(String(128), default="llama3:8b", nullable=False)
    schedule_preset: Mapped[SchedulePreset] = mapped_column(
        Enum(SchedulePreset, name="schedule_preset"),
        default=SchedulePreset.twice_daily,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
