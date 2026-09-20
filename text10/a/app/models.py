from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Fund(Base):
    __tablename__ = "funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    account_type: Mapped[str] = mapped_column(String(16), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "account_type IN ('asset','liability','equity','revenue','expense')",
            name="ck_accounts_type",
        ),
    )


class FiscalPeriod(Base):
    __tablename__ = "fiscal_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    period: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(8), nullable=False, default="open")
    closed_by: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("year", "period", name="uq_fiscal_periods_year_period"),
        CheckConstraint("period BETWEEN 1 AND 12", name="ck_fiscal_periods_period"),
        CheckConstraint("status IN ('open','closed')", name="ck_fiscal_periods_status"),
    )


class PeriodAuditLog(Base):
    """Append-only trail for period close/reopen actions."""

    __tablename__ = "period_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("fiscal_periods.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(8), nullable=False)  # close | reopen
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint("action IN ('close','reopen')", name="ck_period_audit_action"),
    )


class JournalEntry(Base):
    """A posted journal entry. Rows in this table are immutable by convention:

    the API exposes no update/delete, corrections happen via reversal entries
    linked through ``reversal_of_id``.
    """

    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("fiscal_periods.id"), nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")  # manual | csv | reversal
    reversal_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), unique=True
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan", lazy="selectin"
    )
    reversal_of: Mapped["JournalEntry | None"] = relationship(remote_side=[id])


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), nullable=False)
    fund_id: Mapped[int] = mapped_column(ForeignKey("funds.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    debit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")

    __table_args__ = (
        CheckConstraint("debit_cents >= 0", name="ck_journal_lines_debit_nonneg"),
        CheckConstraint("credit_cents >= 0", name="ck_journal_lines_credit_nonneg"),
        CheckConstraint(
            "(debit_cents > 0) <> (credit_cents > 0)",
            name="ck_journal_lines_exactly_one_side",
        ),
    )


class Budget(Base):
    """Budget for one (year, fund, department, account) combination.

    ``encumbered_cents`` and ``actual_cents`` are maintained transactionally
    under a row lock; available = amount - encumbered - actual is kept >= 0
    both in the service layer and by a CHECK constraint as backstop.
    """

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    fund_id: Mapped[int] = mapped_column(ForeignKey("funds.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    encumbered_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint(
            "year", "fund_id", "department_id", "account_id", name="uq_budgets_dims"
        ),
        CheckConstraint("amount_cents >= 0", name="ck_budgets_amount_nonneg"),
        CheckConstraint("encumbered_cents >= 0", name="ck_budgets_encumbered_nonneg"),
        CheckConstraint("actual_cents >= 0", name="ck_budgets_actual_nonneg"),
        CheckConstraint(
            "amount_cents - encumbered_cents - actual_cents >= 0",
            name="ck_budgets_available_nonneg",
        ),
    )


class Encumbrance(Base):
    """An approved spending request reserving budget.

    Status flow: open -> liquidated (journal posted against it, exactly once)
               | open -> cancelled (exactly once)
    """

    __tablename__ = "encumbrances"

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    journal_entry_id: Mapped[int | None] = mapped_column(ForeignKey("journal_entries.id"))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_encumbrances_amount_pos"),
        CheckConstraint(
            "status IN ('open','liquidated','cancelled')", name="ck_encumbrances_status"
        ),
    )
