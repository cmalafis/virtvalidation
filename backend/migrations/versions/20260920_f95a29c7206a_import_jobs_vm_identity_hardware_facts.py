"""import jobs, vm identity and hardware facts

Revision ID: f95a29c7206a
Revises: d4e5f6a7b8c9
Create Date: 2026-09-20 21:40:24.769318

Server-side RVTools ingestion:

  - ``import_jobs`` / ``import_job_rejects`` — durable progress + reject
    report for background imports.
  - ``vms.moref`` / ``vms.vm_uuid`` — vSphere identity. MTV's
    ``plan.spec.vms[].id`` is the MoRef (docs/MTV-GROUNDING.md §4).
  - ``vms`` sizing/placement columns + ``hardware_facts`` JSON.
  - ``vms.name`` stops being unique (``ix_vms_name`` stays as a plain
    index). vSphere names are only unique per folder; identity is
    ``uq_vms_vcenter_moref``, with name-within-vCenter enforced in code
    for MoRef-less rows.
  - Filter/facet indexes for 1,000-5,000 row inventories.

Downgrade note: restoring the global unique index on ``vms.name`` fails
if two VMs now share a name. Resolve the duplicates
first; the downgrade does not pick a winner for you.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f95a29c7206a"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=1024), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=True),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_sheet", sa.String(length=64), nullable=True),
        sa.Column("progress_message", sa.String(length=255), nullable=True),
        sa.Column("rows_total", sa.Integer(), nullable=True),
        sa.Column("rows_read", sa.Integer(), nullable=False),
        sa.Column("rows_valid", sa.Integer(), nullable=False),
        sa.Column("rows_rejected", sa.Integer(), nullable=False),
        sa.Column("rows_warned", sa.Integer(), nullable=False),
        sa.Column("created_count", sa.Integer(), nullable=False),
        sa.Column("updated_count", sa.Integer(), nullable=False),
        sa.Column("unchanged_count", sa.Integer(), nullable=False),
        sa.Column("marked_missing_count", sa.Integer(), nullable=False),
        sa.Column(
            "detected_vcenters",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "vcenter_mapping",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("default_vcenter_id", sa.Integer(), nullable=True),
        sa.Column(
            "result",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("error_message", sa.String(length=2048), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("import_jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_import_jobs_status_created", ["status", "created_at"], unique=False
        )

    op.create_table(
        "import_job_rejects",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("sheet", sa.String(length=64), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("vm_name", sa.String(length=255), nullable=True),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["import_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("import_job_rejects", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_import_job_rejects_job_id"), ["job_id"], unique=False)

    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.add_column(sa.Column("moref", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("vm_uuid", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("power_state", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("esxi_host", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("vsphere_datacenter", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("guest_os_full", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("num_cpus", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("memory_mb", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("disk_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("nic_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("provisioned_mb", sa.BigInteger(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "hardware_facts",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch_op.drop_index("ix_vms_name")
        batch_op.create_index(batch_op.f("ix_vms_name"), ["name"], unique=False)
        batch_op.create_index("ix_vms_environment", ["environment"], unique=False)
        batch_op.create_index("ix_vms_esxi_host", ["esxi_host"], unique=False)
        batch_op.create_index("ix_vms_os_family", ["os_family"], unique=False)
        batch_op.create_index("ix_vms_power_state", ["power_state"], unique=False)
        batch_op.create_index("ix_vms_vsphere_cluster", ["vsphere_cluster"], unique=False)
        batch_op.create_unique_constraint("uq_vms_vcenter_moref", ["source_vcenter_id", "moref"])

def downgrade() -> None:
    with op.batch_alter_table("vms", schema=None) as batch_op:
        batch_op.drop_constraint("uq_vms_vcenter_moref", type_="unique")
        batch_op.drop_index("ix_vms_vsphere_cluster")
        batch_op.drop_index("ix_vms_power_state")
        batch_op.drop_index("ix_vms_os_family")
        batch_op.drop_index("ix_vms_esxi_host")
        batch_op.drop_index("ix_vms_environment")
        batch_op.drop_index(batch_op.f("ix_vms_name"))
        batch_op.create_index("ix_vms_name", ["name"], unique=True)
        batch_op.drop_column("hardware_facts")
        batch_op.drop_column("provisioned_mb")
        batch_op.drop_column("nic_count")
        batch_op.drop_column("disk_count")
        batch_op.drop_column("memory_mb")
        batch_op.drop_column("num_cpus")
        batch_op.drop_column("guest_os_full")
        batch_op.drop_column("vsphere_datacenter")
        batch_op.drop_column("esxi_host")
        batch_op.drop_column("power_state")
        batch_op.drop_column("vm_uuid")
        batch_op.drop_column("moref")

    with op.batch_alter_table("import_job_rejects", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_import_job_rejects_job_id"))

    op.drop_table("import_job_rejects")
    with op.batch_alter_table("import_jobs", schema=None) as batch_op:
        batch_op.drop_index("ix_import_jobs_status_created")

    op.drop_table("import_jobs")
