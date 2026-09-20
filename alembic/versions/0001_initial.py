"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "admin_users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("username", sa.String(length=128), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "ipv4_pools",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("cidr", sa.String(length=43), nullable=False),
        sa.Column("reserved", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_pool_tenant_name"),
    )
    op.create_index("ix_ipv4_pools_tenant_id", "ipv4_pools", ["tenant_id"])
    op.create_table(
        "access_points",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("pool_id", sa.String(length=36), sa.ForeignKey("ipv4_pools.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_ap_tenant_name"),
    )
    op.create_index("ix_access_points_tenant_id", "access_points", ["tenant_id"])
    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("tenant_id", "name", name="uq_device_tenant_name"),
    )
    op.create_index("ix_devices_tenant_id", "devices", ["tenant_id"])
    op.create_table(
        "leases",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("access_point_id", sa.String(length=36), sa.ForeignKey("access_points.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pool_id", sa.String(length=36), sa.ForeignKey("ipv4_pools.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.String(length=36), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ip", sa.String(length=45), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_reason", sa.String(length=32), nullable=True),
        sa.UniqueConstraint("device_id", "idempotency_key", name="uq_lease_device_idem"),
    )
    op.create_index(
        "ux_leases_active_ip",
        "leases",
        ["pool_id", "ip"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_index(
        "ux_leases_active_device",
        "leases",
        ["device_id"],
        unique=True,
        postgresql_where=sa.text("state = 'active'"),
    )
    op.create_index("ix_leases_tenant_state", "leases", ["tenant_id", "state"])
    op.create_index("ix_leases_ap_state", "leases", ["access_point_id", "state"])
    op.create_index("ix_leases_expires_at", "leases", ["expires_at"])


def downgrade() -> None:
    op.drop_table("leases")
    op.drop_table("devices")
    op.drop_table("access_points")
    op.drop_table("ipv4_pools")
    op.drop_table("admin_users")
    op.drop_table("tenants")
