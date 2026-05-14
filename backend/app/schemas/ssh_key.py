"""Pydantic schemas for the multi-key SSH catalog.

The ``SSHKeyRead`` shape exposes only public material — ``public_key``,
``fingerprint``, ``algorithm``, ``status``, ``plan_id``, timestamps. It
does NOT expose ``private_key_path`` or anything derived from the private
key. The endpoint-level test asserts this.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.ssh_key import SSHKeyStatus


class SSHKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    plan_id: int | None = Field(default=None)
    # Algorithm is optional; defaults to the configured ``ssh_key_algorithm``
    # (which respects FIPS mode). One of "ed25519" | "rsa" | "ecdsa".
    algorithm: str | None = Field(default=None, max_length=32)


class SSHKeyRead(BaseModel):
    # ``use_enum_values`` so the API emits ``"active"`` / ``"retired"``
    # rather than ``SSHKeyStatus.active``. ``from_attributes`` lets us
    # populate from an ORM row.
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)

    id: int
    name: str
    public_key: str
    fingerprint: str
    algorithm: str
    status: SSHKeyStatus
    plan_id: int | None
    created_at: datetime
    retired_at: datetime | None


class SSHKeyListResponse(BaseModel):
    """Wrapped list response per the codebase convention."""

    items: list[SSHKeyRead]
    total: int
    skip: int
    limit: int


class SSHKeyRevokeRequest(BaseModel):
    # The wave route resolves these from plan.waves[wave_number-1].vm_ids
    # before calling the service. This shape exists so an operator can
    # also revoke from an arbitrary VM set if needed.
    vm_ids: list[int] = Field(min_length=1)
    force: bool = Field(default=False)


class SSHKeyRevocationOutcome(BaseModel):
    vm_id: int
    succeeded: bool
    detail: str | None = None


class SSHKeyRevokeResponse(BaseModel):
    key_id: int
    total: int
    succeeded: int
    failed: int
    outcomes: list[SSHKeyRevocationOutcome]
