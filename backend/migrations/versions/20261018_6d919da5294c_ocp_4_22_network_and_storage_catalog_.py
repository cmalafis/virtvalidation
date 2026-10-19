"""ocp 4.22 network and storage catalog attributes

Revision ID: 6d919da5294c
Revises: 5e6edea4d493
Create Date: 2026-10-18 21:40:12.270361

OpenShift 4.21/4.22 network and storage attributes on the per-cluster
catalogs, so a mapping can say what MTV destination it needs:

- ``target_networks``: OVN-Kubernetes role / topology / VLAN /
  physicalNetworkName / MTU / subnets / IPAM mode / binding / transport.
  A primary UDN/CUDN is emitted as NetworkMap ``type: pod``; everything
  else stays ``multus``.
- ``target_storage_classes``: volumeMode, CSI provisioner, and the MTV
  copy-offload (vsphereXcopyConfig) vendor + Secret.
- ``ocp_target_namespaces.primary_udn_network``: the primary UDN serving
  the namespace, if any.
- ``ocp_targets.mtv_version`` and ``localnet_bridge_mappings`` (NNCP
  localnet names captured by the catalog import).

All columns are nullable and no backfill is needed: NULL ``role`` keeps
today's behaviour (non-pod networks emit ``multus``).

Autogenerate again proposed the unrelated ``app_settings.active_llm_backend``
enum retype and ``vms`` index rename (pre-existing drift, see 5e6edea4d493).
Both were dropped on purpose.
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = '6d919da5294c'
down_revision: str | Sequence[str] | None = '5e6edea4d493'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table('ocp_target_namespaces', schema=None) as batch_op:
        batch_op.add_column(sa.Column('primary_udn_network', sa.String(length=128), nullable=True))

    with op.batch_alter_table('ocp_targets', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mtv_version', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('localnet_bridge_mappings', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))

    with op.batch_alter_table('target_networks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('topology', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('vlan_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('physical_network_name', sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column('mtu', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('subnets', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))
        batch_op.add_column(sa.Column('ipam_mode', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('binding', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('transport', sa.String(length=16), nullable=True))

    with op.batch_alter_table('target_storage_classes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('volume_mode', sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column('provisioner', sa.String(length=253), nullable=True))
        batch_op.add_column(sa.Column('copy_offload_vendor', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('copy_offload_secret', sa.String(length=253), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('target_storage_classes', schema=None) as batch_op:
        batch_op.drop_column('copy_offload_secret')
        batch_op.drop_column('copy_offload_vendor')
        batch_op.drop_column('provisioner')
        batch_op.drop_column('volume_mode')

    with op.batch_alter_table('target_networks', schema=None) as batch_op:
        batch_op.drop_column('transport')
        batch_op.drop_column('binding')
        batch_op.drop_column('ipam_mode')
        batch_op.drop_column('subnets')
        batch_op.drop_column('mtu')
        batch_op.drop_column('physical_network_name')
        batch_op.drop_column('vlan_id')
        batch_op.drop_column('topology')
        batch_op.drop_column('role')

    with op.batch_alter_table('ocp_targets', schema=None) as batch_op:
        batch_op.drop_column('localnet_bridge_mappings')
        batch_op.drop_column('mtv_version')

    with op.batch_alter_table('ocp_target_namespaces', schema=None) as batch_op:
        batch_op.drop_column('primary_udn_network')

