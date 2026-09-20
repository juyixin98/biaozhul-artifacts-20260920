"""initial schema

Revision ID: 0001
Create Date: 2026-09-20
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "funds",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
    )
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
    )
    op.create_table(
        "periods",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.UniqueConstraint("year", "month", name="uq_period_year_month"),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("idempotency_key", sa.String(128), unique=True),
        sa.Column("request_hash", sa.String(64)),
        sa.Column("period_id", sa.Integer, sa.ForeignKey("periods.id"), nullable=False),
        sa.Column("description", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="posted"),
        sa.Column("reversal_of_id", sa.Integer,
                  sa.ForeignKey("journal_entries.id"), unique=True),
        sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("entry_id", sa.Integer, sa.ForeignKey("journal_entries.id"),
                  nullable=False, index=True),
        sa.Column("fund_id", sa.Integer, sa.ForeignKey("funds.id"), nullable=False),
        sa.Column("department_id", sa.Integer, sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("debit_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("credit_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.CheckConstraint("debit_cents >= 0", name="ck_line_debit_nonneg"),
        sa.CheckConstraint("credit_cents >= 0", name="ck_line_credit_nonneg"),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("fund_id", sa.Integer, sa.ForeignKey("funds.id"), nullable=False),
        sa.Column("department_id", sa.Integer, sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("amount_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("encumbered_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("actual_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.UniqueConstraint("year", "fund_id", "department_id", "account_id",
                            name="uq_budget_scope"),
        sa.CheckConstraint("amount_cents >= 0", name="ck_budget_amount_nonneg"),
        sa.CheckConstraint("encumbered_cents >= 0", name="ck_budget_enc_nonneg"),
        sa.CheckConstraint("actual_cents >= 0", name="ck_budget_act_nonneg"),
    )
    op.create_table(
        "expenditure_requests",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("budget_id", sa.Integer, sa.ForeignKey("budgets.id"), nullable=False),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("purpose", sa.String(500), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.String(100), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="ck_request_amount_pos"),
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in ("audit_log", "expenditure_requests", "budgets", "journal_lines",
                  "journal_entries", "periods", "accounts", "departments", "funds"):
        op.drop_table(table)
