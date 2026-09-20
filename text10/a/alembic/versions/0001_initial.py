"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-19

"""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "funds",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
    )
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("account_type", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "account_type IN ('asset','liability','equity','revenue','expense')",
            name="ck_accounts_type",
        ),
    )
    op.create_table(
        "fiscal_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("period", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False, server_default="open"),
        sa.Column("closed_by", sa.String(64)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("year", "period", name="uq_fiscal_periods_year_period"),
        sa.CheckConstraint("period BETWEEN 1 AND 12", name="ck_fiscal_periods_period"),
        sa.CheckConstraint("status IN ('open','closed')", name="ck_fiscal_periods_status"),
    )
    op.create_table(
        "period_audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("period_id", sa.Integer(), sa.ForeignKey("fiscal_periods.id"), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("action IN ('close','reopen')", name="ck_period_audit_action"),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("period_id", sa.Integer(), sa.ForeignKey("fiscal_periods.id"), nullable=False),
        sa.Column("memo", sa.Text(), nullable=False, server_default=""),
        sa.Column("source", sa.String(16), nullable=False, server_default="manual"),
        sa.Column(
            "reversal_of_id",
            sa.Integer(),
            sa.ForeignKey("journal_entries.id"),
            unique=True,
        ),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id"), nullable=False),
        sa.Column("fund_id", sa.Integer(), sa.ForeignKey("funds.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("debit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("credit_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint("debit_cents >= 0", name="ck_journal_lines_debit_nonneg"),
        sa.CheckConstraint("credit_cents >= 0", name="ck_journal_lines_credit_nonneg"),
        sa.CheckConstraint(
            "(debit_cents > 0) <> (credit_cents > 0)",
            name="ck_journal_lines_exactly_one_side",
        ),
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("fund_id", sa.Integer(), sa.ForeignKey("funds.id"), nullable=False),
        sa.Column("department_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=False),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("accounts.id"), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("encumbered_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("actual_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "year", "fund_id", "department_id", "account_id", name="uq_budgets_dims"
        ),
        sa.CheckConstraint("amount_cents >= 0", name="ck_budgets_amount_nonneg"),
        sa.CheckConstraint("encumbered_cents >= 0", name="ck_budgets_encumbered_nonneg"),
        sa.CheckConstraint("actual_cents >= 0", name="ck_budgets_actual_nonneg"),
        sa.CheckConstraint(
            "amount_cents - encumbered_cents - actual_cents >= 0",
            name="ck_budgets_available_nonneg",
        ),
    )
    op.create_table(
        "encumbrances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("budget_id", sa.Integer(), sa.ForeignKey("budgets.id"), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("journal_entry_id", sa.Integer(), sa.ForeignKey("journal_entries.id")),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("amount_cents > 0", name="ck_encumbrances_amount_pos"),
        sa.CheckConstraint(
            "status IN ('open','liquidated','cancelled')", name="ck_encumbrances_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("encumbrances")
    op.drop_table("budgets")
    op.drop_table("journal_lines")
    op.drop_table("journal_entries")
    op.drop_table("period_audit_logs")
    op.drop_table("fiscal_periods")
    op.drop_table("accounts")
    op.drop_table("departments")
    op.drop_table("funds")
