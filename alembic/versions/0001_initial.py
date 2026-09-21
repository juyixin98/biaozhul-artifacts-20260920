"""initial schema: users, wallets, drafts, sign_requests, audit_logs

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("api_key_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("api_key_hash", name="uq_users_api_key_hash"),
    )
    op.create_index("ix_users_api_key_hash", "users", ["api_key_hash"])

    op.create_table(
        "wallets",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("address", sa.String(length=42), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("key_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "address", name="uq_wallets_user_address"),
    )
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"])

    op.create_table(
        "drafts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("to_address", sa.String(length=42), nullable=False),
        sa.Column("value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("gas", sa.BigInteger(), nullable=False),
        sa.Column("gas_price_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("nonce", sa.BigInteger(), nullable=False),
        sa.Column("data_hex", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_drafts_user_id", "drafts", ["user_id"])
    op.create_index("ix_drafts_wallet_id", "drafts", ["wallet_id"])

    op.create_table(
        "sign_requests",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "draft_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("drafts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("to_address", sa.String(length=42), nullable=False),
        sa.Column("value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("gas", sa.BigInteger(), nullable=False),
        sa.Column("gas_price_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("nonce", sa.BigInteger(), nullable=False),
        sa.Column("data_hex", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("raw_transaction_hex", sa.Text(), nullable=True),
        sa.Column("tx_hash", sa.String(length=66), nullable=True),
        sa.Column("error_reason", sa.String(length=256), nullable=True),
        sa.Column("quota_day", sa.String(length=10), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_signrequests_user_idem"),
        sa.CheckConstraint(
            "status IN ('pending', 'success', 'failed', 'released')",
            name="ck_signrequests_status",
        ),
        sa.CheckConstraint(
            "value_wei >= 0 AND gas_price_wei >= 0 AND gas > 0",
            name="ck_signrequests_nonneg",
        ),
    )
    # Partial unique index: at most one occupying (pending/success) request per
    # wallet + chain + nonce. Failed/released rows free the nonce.
    op.create_index(
        "uq_signrequests_nonce_occupying",
        "sign_requests",
        ["wallet_id", "chain_id", "nonce"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'success')"),
    )
    op.create_index("ix_signrequests_user_id", "sign_requests", ["user_id"])
    op.create_index("ix_signrequests_wallet_id", "sign_requests", ["wallet_id"])
    op.create_index("ix_signrequests_quota_day", "sign_requests", ["quota_day"])
    op.create_index("ix_signrequests_created_at", "sign_requests", ["created_at"])

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_request_id", sa.String(length=36), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("wallet_id", sa.String(length=36), nullable=True),
        sa.Column("sign_request_id", sa.String(length=36), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("detail", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("sign_requests")
    op.drop_table("drafts")
    op.drop_table("wallets")
    op.drop_table("users")
