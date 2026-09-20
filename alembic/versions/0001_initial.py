"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "funds",
        sa.Column("code", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
    )
    op.create_table(
        "departments",
        sa.Column("code", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
    )
    op.create_table(
        "accounts",
        sa.Column("code", sa.String(length=32), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("account_class", sa.Integer, nullable=False),
        sa.Column("normal_side", sa.String(length=1), nullable=False),
        sa.CheckConstraint("account_class BETWEEN 1 AND 5", name="accounts_class_chk"),
        sa.CheckConstraint("normal_side IN ('D','C')", name="accounts_side_chk"),
    )
    op.create_table(
        "periods",
        sa.Column("code", sa.String(length=7), primary_key=True),
        sa.Column("start_date", sa.Date, nullable=False),
        sa.Column("end_date", sa.Date, nullable=False),
        sa.Column(
            "is_closed", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column("closed_reason", sa.String(length=1000)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("closed_by", sa.String(length=64)),
        sa.CheckConstraint("end_date >= start_date", name="periods_dates_chk"),
    )
    op.create_table(
        "period_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "period_code",
            sa.String(length=7),
            sa.ForeignKey("periods.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("actor", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "action IN ('close','reopen')", name="period_events_action_chk"
        ),
    )
    op.create_index(
        "ix_period_events_period", "period_events", ["period_code", "created_at"]
    )
    op.create_table(
        "budgets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column(
            "fund_code",
            sa.String(length=32),
            sa.ForeignKey("funds.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "department_code",
            sa.String(length=32),
            sa.ForeignKey("departments.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_code",
            sa.String(length=32),
            sa.ForeignKey("accounts.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column(
            "reserved_cents",
            sa.BigInteger,
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "actual_cents", sa.BigInteger, nullable=False, server_default="0"
        ),
        sa.Column(
            "version", sa.BigInteger, nullable=False, server_default="1"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "year",
            "fund_code",
            "department_code",
            "account_code",
            name="budgets_scope_uq",
        ),
        sa.CheckConstraint("amount_cents >= 0", name="budgets_amount_chk"),
        sa.CheckConstraint("reserved_cents >= 0", name="budgets_reserved_chk"),
        sa.CheckConstraint("actual_cents >= 0", name="budgets_actual_chk"),
    )
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("voucher_no", sa.String(length=64), nullable=False, unique=True),
        sa.Column("entry_date", sa.Date, nullable=False),
        sa.Column(
            "period_code",
            sa.String(length=7),
            sa.ForeignKey("periods.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("description", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column(
            "is_reversal", sa.Boolean, nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "reverses_entry_id",
            sa.BigInteger,
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        ),
        sa.Column("reversal_reason", sa.String(length=1000)),
        sa.Column("created_by", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_journal_entries_period", "journal_entries", ["period_code"])
    op.create_index("ix_journal_entries_date", "journal_entries", ["entry_date"])
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("request_no", sa.String(length=64), nullable=False, unique=True),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column(
            "fund_code",
            sa.String(length=32),
            sa.ForeignKey("funds.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "department_code",
            sa.String(length=32),
            sa.ForeignKey("departments.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_code",
            sa.String(length=32),
            sa.ForeignKey("accounts.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount_cents", sa.BigInteger, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="approved"),
        sa.Column("description", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("approved_by", sa.String(length=64), nullable=False),
        sa.Column(
            "approved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_by", sa.String(length=64)),
        sa.Column("cancel_reason", sa.String(length=1000)),
        sa.Column(
            "consumed_entry_id",
            sa.BigInteger,
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "reversed_entry_id",
            sa.BigInteger,
            sa.ForeignKey("journal_entries.id", ondelete="RESTRICT"),
        ),
        sa.CheckConstraint("amount_cents > 0", name="budget_reservations_amount_chk"),
        sa.CheckConstraint(
            "status IN ('approved','consumed','cancelled','reversed')",
            name="budget_reservations_status_chk",
        ),
    )
    op.create_index(
        "ix_budget_reservations_scope",
        "budget_reservations",
        ["year", "fund_code", "department_code", "account_code"],
    )
    op.create_table(
        "journal_lines",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "entry_id",
            sa.BigInteger,
            sa.ForeignKey("journal_entries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_no", sa.Integer, nullable=False),
        sa.Column(
            "fund_code",
            sa.String(length=32),
            sa.ForeignKey("funds.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "department_code",
            sa.String(length=32),
            sa.ForeignKey("departments.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "account_code",
            sa.String(length=32),
            sa.ForeignKey("accounts.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("debit_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("credit_cents", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "reservation_id",
            sa.BigInteger,
            sa.ForeignKey("budget_reservations.id", ondelete="RESTRICT"),
        ),
        sa.Column("description", sa.String(length=1000), nullable=False, server_default=""),
        sa.UniqueConstraint("entry_id", "line_no", name="journal_lines_entry_line_uq"),
        sa.CheckConstraint("debit_cents >= 0", name="journal_lines_debit_chk"),
        sa.CheckConstraint("credit_cents >= 0", name="journal_lines_credit_chk"),
        sa.CheckConstraint(
            "(debit_cents > 0) <> (credit_cents > 0)",
            name="journal_lines_one_side_chk",
        ),
    )
    op.create_table(
        "idempotent_ops",
        sa.Column("key", sa.String(length=128), primary_key=True),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status_code", sa.Integer, nullable=False),
        sa.Column("response_body", sa.String, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotent_ops")
    op.drop_table("journal_lines")
    op.drop_index("ix_budget_reservations_scope", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_index("ix_journal_entries_date", table_name="journal_entries")
    op.drop_index("ix_journal_entries_period", table_name="journal_entries")
    op.drop_table("journal_entries")
    op.drop_table("budgets")
    op.drop_index("ix_period_events_period", table_name="period_events")
    op.drop_table("period_events")
    op.drop_table("periods")
    op.drop_table("accounts")
    op.drop_table("departments")
    op.drop_table("funds")
