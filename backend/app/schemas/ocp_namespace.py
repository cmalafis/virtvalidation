"""Pydantic schemas for the operator-declared OCPTargetNamespace
catalog. Mirrors :mod:`app.schemas.target_entities` patterns one-for-
one — the namespaces tab on the OCP Target detail page consumes the
same paginated shape as the networks / storage tabs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class OCPTargetNamespaceBase(BaseModel):
    name: str = Field(min_length=1, max_length=253)
    description: str | None = Field(default=None, max_length=4096)


class OCPTargetNamespaceCreate(OCPTargetNamespaceBase):
    pass


class OCPTargetNamespaceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=253)
    description: str | None = Field(default=None, max_length=4096)


class OCPTargetNamespaceRead(OCPTargetNamespaceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    ocp_target_id: int
    created_at: datetime
    updated_at: datetime


class OCPTargetNamespaceListResponse(BaseModel):
    """Paginated wrapper — same shape as VMListResponse and the rest of
    the listing endpoints per the CLAUDE.md pagination rule."""

    items: list[OCPTargetNamespaceRead]
    total: int
    skip: int
    limit: int


class OCPTargetNamespaceDeleteConflict(BaseModel):
    """409 body shape returned when a namespace is referenced by a VM
    override or a mapping's namespace strategy."""

    detail: str
    referenced_by: list[dict] = Field(default_factory=list)
