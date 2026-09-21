"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("api_key", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_users_api_key", "users", ["api_key"], unique=True)

    op.create_table(
        "wallets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("address", sa.String(42), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("encrypted_key", sa.Text, nullable=True),
        sa.Column("last_signed_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"])

    op.create_table(
        "drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("wallet_id", sa.String(36), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("chain_id", sa.Integer, nullable=False),
        sa.Column("to_address", sa.String(42), nullable=False),
        sa.Column("value_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("gas_limit", sa.BigInteger, nullable=False),
        sa.Column("gas_price_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("nonce", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("submitted_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_drafts_user_id", "drafts", ["user_id"])
    op.create_index("ix_drafts_wallet_id", "drafts", ["wallet_id"])

    op.create_table(
        "sign_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("wallet_id", sa.String(36), sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("draft_id", sa.String(36), sa.ForeignKey("drafts.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("chain_id", sa.Integer, nullable=False),
        sa.Column("nonce", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("signed_tx", sa.Text, nullable=True),
        sa.Column("tx_hash", sa.String(66), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("quota_reserved_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("quota_released", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_sign_idempotency"),
    )
    op.create_index("ix_sign_requests_user_id", "sign_requests", ["user_id"])
    op.create_index(
        "ix_sign_wallet_chain_nonce", "sign_requests", ["wallet_id", "chain_id", "nonce"]
    )

    op.create_table(
        "daily_usage",
        sa.Column("wallet_id", sa.String(36), sa.ForeignKey("wallets.id"), primary_key=True),
        sa.Column("day", sa.String(10), primary_key=True),
        sa.Column("used_wei", sa.Numeric(78, 0), nullable=False),
        sa.Column("limit_wei", sa.Numeric(78, 0), nullable=False),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("wallet_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("tx_digest", sa.String(66), nullable=True),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_audit_log_user_id", "audit_log", ["user_id"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("daily_usage")
    op.drop_table("sign_requests")
    op.drop_table("drafts")
    op.drop_table("wallets")
    op.drop_table("users")
