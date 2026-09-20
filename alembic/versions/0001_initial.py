"""initial schema for CloudGate

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

# Enums are created explicitly below (idempotently); create_type=False stops
# the PG dialect from re-issuing CREATE TYPE when tables reference them.
lease_status_enum = postgresql.ENUM("active", "closed", name="lease_status", create_type=False)
lease_reason_enum = postgresql.ENUM(
    "connected", "device_disconnect", "expired", "revoked", name="lease_reason", create_type=False
)


def upgrade() -> None:
    # CREATE TYPE can be autocommitted by drivers (psycopg2), so make enum
    # creation idempotent: a partially-applied migration can be safely re-run.
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lease_status') THEN "
        "CREATE TYPE lease_status AS ENUM ('active', 'closed'); END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        "IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'lease_reason') THEN "
        "CREATE TYPE lease_reason AS ENUM ('connected', 'device_disconnect', 'expired', 'revoked'); "
        "END IF; END $$"
    )


    op.create_table(
        "tenants",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("admin_key_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "address_pools",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("cidr", sa.String(length=64), nullable=False),
        sa.Column("reserved_ips", postgresql.ARRAY(sa.Text()), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_pool_tenant_name"),
    )

    op.create_table(
        "access_points",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("pool_id", sa.BigInteger(), sa.ForeignKey("address_pools.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_ap_tenant_name"),
    )

    op.create_table(
        "devices",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("tenant_id", "name", name="uq_device_tenant_name"),
    )

    op.create_table(
        "leases",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("tenant_id", sa.BigInteger(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("device_id", sa.BigInteger(), sa.ForeignKey("devices.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "access_point_id",
            sa.BigInteger(),
            sa.ForeignKey("access_points.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ip_address", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("status", lease_status_enum, nullable=False, server_default="active"),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", lease_reason_enum, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_leases_tenant_id", "leases", ["tenant_id"])
    op.create_index("ix_leases_access_point_id", "leases", ["access_point_id"])
    op.create_index("ix_leases_expires_at", "leases", ["expires_at"])
    op.create_index("ix_leases_device_status", "leases", ["device_id", "status"])

    # Hard concurrency guarantees (see app/models.py):
    op.execute(
        "CREATE UNIQUE INDEX uq_active_lease_device ON leases (device_id) WHERE status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_active_lease_ap_ip ON leases (access_point_id, ip_address) WHERE status = 'active'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_lease_device_idempotency_key ON leases (device_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )

    op.create_table(
        "lease_events",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("lease_id", sa.BigInteger(), sa.ForeignKey("leases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("reason", lease_reason_enum, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("detail", sa.Text(), nullable=True),
    )
    op.create_index("ix_lease_events_lease_id", "lease_events", ["lease_id"])


def downgrade() -> None:
    op.drop_table("lease_events")
    op.execute("DROP INDEX IF EXISTS uq_lease_device_idempotency_key")
    op.execute("DROP INDEX IF EXISTS uq_active_lease_ap_ip")
    op.execute("DROP INDEX IF EXISTS uq_active_lease_device")
    op.drop_index("ix_leases_device_status", table_name="leases")
    op.drop_index("ix_leases_expires_at", table_name="leases")
    op.drop_index("ix_leases_access_point_id", table_name="leases")
    op.drop_index("ix_leases_tenant_id", table_name="leases")
    op.drop_table("leases")
    op.drop_table("devices")
    op.drop_table("access_points")
    op.drop_table("address_pools")
    op.drop_table("tenants")
    op.execute("DROP TYPE lease_reason")
    op.execute("DROP TYPE lease_status")
