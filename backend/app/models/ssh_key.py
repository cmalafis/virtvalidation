"""Multi-key SSH key catalog for the wave-scoped validation flow.

Coexists with the singleton appliance key in :mod:`app.core.ssh_key`.
The singleton remains the appliance's self-identity key (system-wide,
rotatable). The rows here are *named* per-plan keys an operator generates
for baseline + validation runs on a specific migration wave, so the same
private key isn't authorized on every VM the appliance ever touches.

The **private key never enters this table**. Only the filesystem path
(:attr:`SSHKey.private_key_path`) is stored. The bytes live on the
``/app/keys/`` PVC at ``/app/keys/named/<uuid>`` with mode 0600 and never
appear in any API response.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class SSHKeyStatus(str, enum.Enum):
    active = "active"
    retired = "retired"


class SSHKey(Base):
    """An Ed25519 (or other) keypair the appliance generated for use by the
    wave-scoped baseline + validation flow.

    Retirement keeps the private key file on disk for an audit window — the
    operator may need to prove what key was authorized on which VMs. A
    separate cleanup job (out of scope here) can purge retired keys after N
    days.
    """

    __tablename__ = "ssh_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # OpenSSH public key string (``ssh-ed25519 AAAA... comment``). Safe to
    # expose via API; this is what operators paste into authorized_keys.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Filesystem path on the appliance PVC. NEVER the bytes.
    private_key_path: Mapped[str] = mapped_column(String(512), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False, default="ed25519")
    status: Mapped[SSHKeyStatus] = mapped_column(
        Enum(SSHKeyStatus, name="ssh_key_status"),
        default=SSHKeyStatus.active,
        server_default=SSHKeyStatus.active.value,
        nullable=False,
        index=True,
    )
    # Optional scoping to a specific plan. NULL = "any plan can use it".
    # SET NULL on delete so the key survives plan deletion (audit-friendly);
    # the operator can still retire it separately.
    plan_id: Mapped[int | None] = mapped_column(
        ForeignKey("migration_plans.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_ssh_keys_status_plan", "status", "plan_id"),)
