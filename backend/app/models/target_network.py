"""Operator-defined target network entities on an OCP cluster.

Each row represents one NetworkAttachmentDefinition / CUDN / UDN that
the operator declares is available on the target cluster. The mapping
editor dropdowns are populated from these rows, not from live cluster
discovery — the design is "operator tells the appliance what's there"
because air-gapped deployments often can't reach the cluster API.

The OVN-Kubernetes attributes (``role``, ``topology``, ``ipam_mode``, …)
can be typed by hand or filled by the catalog import
(:mod:`app.core.catalog_import`), which parses pasted
``oc get … -o yaml`` output. They decide what MTV destination a mapping
emits: a primary UDN/CUDN is ``type: pod`` in a NetworkMap, everything
else is ``multus``. See docs/MTV-GROUNDING.md §2.1 and §12.

The new attributes are plain strings validated by the Pydantic schema,
not Postgres enums: their value sets track OVN-Kubernetes releases
(EVPN arrived in 4.22), and widening a Postgres enum needs a
non-transactional ``ALTER TYPE`` for every addition.
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
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, JSONType


class TargetNetworkType(str, enum.Enum):
    nad = "nad"
    cudn = "cudn"
    udn = "udn"
    pod = "pod"


class TargetNetwork(Base):
    __tablename__ = "target_networks"

    id: Mapped[int] = mapped_column(primary_key=True)
    ocp_target_id: Mapped[int] = mapped_column(
        ForeignKey("ocp_targets.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    network_type: Mapped[TargetNetworkType] = mapped_column(
        Enum(TargetNetworkType, name="target_network_type"), nullable=False
    )
    namespace: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # primary | secondary. Only meaningful for udn/cudn; a NAD is always
    # a secondary network and ``pod`` is the cluster default network.
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # layer2 | layer3 | localnet | bridge | sriov | other
    topology: Mapped[str | None] = mapped_column(String(16), nullable=True)
    vlan_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Localnet only: must match an NNCP ovn.bridge-mappings[].localnet.
    physical_network_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    mtu: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # CIDR strings, e.g. ["10.100.0.0/16"]. Primary UDNs need them for the
    # preserve-static-IP check.
    subnets: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    # persistent | disabled | none
    ipam_mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # l2bridge | passt | bridge | sriov — the VM interface binding.
    binding: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # geneve | evpn (4.22+, primary CUDN only)
    transport: Mapped[str | None] = mapped_column(String(16), nullable=True)
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
        UniqueConstraint("ocp_target_id", "name", name="uq_target_network_per_cluster"),
        Index("ix_target_network_ocp_target", "ocp_target_id"),
    )
