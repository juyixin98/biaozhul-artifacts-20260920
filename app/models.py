from __future__ import annotations

from datetime import date

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Fund(Base):
    __tablename__ = "funds"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Department(Base):
    __tablename__ = "departments"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class Account(Base):
    __tablename__ = "accounts"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # 5 == expense. Expense lines consume budgets.
    account_class: Mapped[int] = mapped_column(nullable=False)
    # Normal debit side of the account class. Expense lines post budgets on debit.
    normal_side: Mapped[str] = mapped_column(String(1), nullable=False)

    __table_args__ = (
        CheckConstraint("account_class BETWEEN 1 AND 5", name="accounts_class_chk"),
        CheckConstraint("normal_side IN ('D','C')", name="accounts_side_chk"),
    )


class Period(Base):
    __tablename__ = "periods"

    code: Mapped[str] = mapped_column(String(7), primary_key=True)  # e.g. 2026-09
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_closed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    closed_reason: Mapped[str | None] = mapped_column(String(1000))
    closed_at: Mapped[date | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="periods_dates_chk"),
    )


class PeriodEvent(Base):
    __tablename__ = "period_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    period_code: Mapped[str] = mapped_column(
        ForeignKey("periods.code", ondelete="RESTRICT"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # close / reopen
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[date] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint("action IN ('close','reopen')", name="period_events_action_chk"),
        Index("ix_period_events_period", "period_code", "created_at"),
    )


class Budget(Base):
    """Annual budget keyed by fund + department + expense account."""

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    year: Mapped[int] = mapped_column(nullable=False)
    fund_code: Mapped[str] = mapped_column(
        ForeignKey("funds.code", ondelete="RESTRICT"), nullable=False
    )
    department_code: Mapped[str] = mapped_column(
        ForeignKey("departments.code", ondelete="RESTRICT"), nullable=False
    )
    account_code: Mapped[str] = mapped_column(
        ForeignKey("accounts.code", ondelete="RESTRICT"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Denormalised counters; the invariant reserved+actual <= amount is enforced
    # in service code under SELECT ... FOR UPDATE row locks.
    reserved_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[date] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[date] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "year",
            "fund_code",
            "department_code",
            "account_code",
            name="budgets_scope_uq",
        ),
        CheckConstraint("amount_cents >= 0", name="budgets_amount_chk"),
        CheckConstraint("reserved_cents >= 0", name="budgets_reserved_chk"),
        CheckConstraint("actual_cents >= 0", name="budgets_actual_chk"),
    )


class JournalEntry(Base):
    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    voucher_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    entry_date: Mapped[date] = mapped_column(Date, nullable=False)
    period_code: Mapped[str] = mapped_column(
        ForeignKey("periods.code", ondelete="RESTRICT"), nullable=False
    )
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    is_reversal: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reverses_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    reversal_reason: Mapped[str | None] = mapped_column(String(1000))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[date] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry",
        cascade="all, delete-orphan",
        order_by="JournalLine.line_no",
    )
    # On the reversal row this points at the original voucher.
    reverses: Mapped["JournalEntry | None"] = relationship(
        remote_side=[id],
        foreign_keys=[reverses_entry_id],
        back_populates="reversed_by",
    )
    # View from the original voucher to its (single) reversal.
    reversed_by: Mapped["JournalEntry | None"] = relationship(
        foreign_keys=[reverses_entry_id],
        back_populates="reverses",
        uselist=False,
        viewonly=True,
    )

    __table_args__ = (
        Index("ix_journal_entries_period", "period_code"),
        Index("ix_journal_entries_date", "entry_date"),
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="CASCADE"), nullable=False
    )
    line_no: Mapped[int] = mapped_column(nullable=False)
    fund_code: Mapped[str] = mapped_column(
        ForeignKey("funds.code", ondelete="RESTRICT"), nullable=False
    )
    department_code: Mapped[str] = mapped_column(
        ForeignKey("departments.code", ondelete="RESTRICT"), nullable=False
    )
    account_code: Mapped[str] = mapped_column(
        ForeignKey("accounts.code", ondelete="RESTRICT"), nullable=False
    )
    debit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    reservation_id: Mapped[int | None] = mapped_column(
        ForeignKey("budget_reservations.id", ondelete="RESTRICT")
    )
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")
    reservation: Mapped["BudgetReservation | None"] = relationship(
        foreign_keys=[reservation_id]
    )

    __table_args__ = (
        UniqueConstraint("entry_id", "line_no", name="journal_lines_entry_line_uq"),
        CheckConstraint("debit_cents >= 0", name="journal_lines_debit_chk"),
        CheckConstraint("credit_cents >= 0", name="journal_lines_credit_chk"),
        CheckConstraint(
            "(debit_cents > 0) <> (credit_cents > 0)",
            name="journal_lines_one_side_chk",
        ),
    )


class BudgetReservation(Base):
    """An approved expenditure request that pre-occupies budget."""

    __tablename__ = "budget_reservations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    request_no: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    year: Mapped[int] = mapped_column(nullable=False)
    fund_code: Mapped[str] = mapped_column(
        ForeignKey("funds.code", ondelete="RESTRICT"), nullable=False
    )
    department_code: Mapped[str] = mapped_column(
        ForeignKey("departments.code", ondelete="RESTRICT"), nullable=False
    )
    account_code: Mapped[str] = mapped_column(
        ForeignKey("accounts.code", ondelete="RESTRICT"), nullable=False
    )
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # approved -> consumed (on posting) or cancelled; consumed -> reversed.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="approved")
    description: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    approved_by: Mapped[str] = mapped_column(String(64), nullable=False)
    approved_at: Mapped[date] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    cancelled_at: Mapped[date | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(64))
    cancel_reason: Mapped[str | None] = mapped_column(String(1000))
    consumed_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )
    reversed_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id", ondelete="RESTRICT")
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="budget_reservations_amount_chk"),
        CheckConstraint(
            "status IN ('approved','consumed','cancelled','reversed')",
            name="budget_reservations_status_chk",
        ),
        Index(
            "ix_budget_reservations_scope",
            "year",
            "fund_code",
            "department_code",
            "account_code",
        ),
    )


class IdempotentOp(Base):
    __tablename__ = "idempotent_ops"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(nullable=False)
    response_body: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[date] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
