import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.core.llm.types import LLMBackendType


class SchedulePreset(str, enum.Enum):
    twice_daily = "twice_daily"  # 06:00 and 18:00 UTC
    once_daily = "once_daily"  # 06:00 UTC
    hourly = "hourly"  # every hour at :00


class SSHHostKeyPolicy(str, enum.Enum):
    """How the SSH collector handles unknown host keys.

    ``auto_accept`` is the SSH "trust on first use" model — first
    connection to a VM auto-accepts the host key and persists it; every
    subsequent connection verifies against the stored key. Suitable for
    most environments.

    ``strict`` rejects any host whose key isn't already in
    ``known_hosts`` — federal classified mode where every key must be
    distributed via an out-of-band process.
    """

    auto_accept = "auto_accept"
    strict = "strict"


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
    ssh_host_key_policy: Mapped[SSHHostKeyPolicy] = mapped_column(
        Enum(SSHHostKeyPolicy, name="ssh_host_key_policy"),
        default=SSHHostKeyPolicy.auto_accept,
        server_default=SSHHostKeyPolicy.auto_accept.value,
        nullable=False,
    )
    # Active LLM backend — runtime-switchable from the Settings UI.
    # Replaces the env-var-driven selection ``LLM_BACKEND_TYPE``, which
    # is now used only as the migration's bootstrap seed value.
    # ``values_callable`` because LLMBackendType has names == values
    # (so default behavior would also work) but we keep the kwarg
    # explicit per CLAUDE.md's enum I/O rule for any enum that crosses
    # the SQLAlchemy boundary.
    active_llm_backend: Mapped[LLMBackendType] = mapped_column(
        Enum(
            LLMBackendType,
            name="llm_backend_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        default=LLMBackendType.mock,
        server_default=LLMBackendType.mock.value,
        nullable=False,
    )
    # Last-observed LLM error surfaced in the Settings UI as a
    # persistent banner. Populated specifically by the auth-failure
    # path (and any other "fall back BUT make sure the operator sees
    # it" path) — cleared on the next successful LLM call or
    # successful test_connection. The mechanical fallback for transient
    # failures does NOT write here; it stays quiet per the existing
    # validate-retry-fallback discipline.
    last_llm_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_llm_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
