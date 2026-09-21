from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Fund(Base):
    __tablename__ = "funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Period(Base):
    __tablename__ = "periods"
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_period_year_month"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_period_month"),
        CheckConstraint("status IN ('open', 'closed')", name="ck_period_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(8), default="open", nullable=False)
    # Bumped by every open-guard/close/reopen update so posting and closing
    # serialize on this row (explicit ordering between post and close).
    lock_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_by: Mapped[str | None] = mapped_column(String(128))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PeriodAudit(Base):
    """Append-only trail of close/reopen actions (who, when, why)."""

    __tablename__ = "period_audits"
    __table_args__ = (
        CheckConstraint("action IN ('close', 'reopen')", name="ck_period_audit_action"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("periods.id"), index=True)
    action: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JournalEntry(Base):
    """A posted voucher. Posted entries are immutable: there are deliberately
    no update/delete endpoints; corrections happen through reversal entries."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        UniqueConstraint("period_id", "voucher_no", name="uq_entry_period_voucher"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    voucher_no: Mapped[str] = mapped_column(String(64))
    period_id: Mapped[int] = mapped_column(ForeignKey("periods.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    memo: Mapped[str] = mapped_column(Text, default="")
    reversal_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), unique=True
    )
    expense_request_id: Mapped[int | None] = mapped_column(
        ForeignKey("expense_requests.id"), unique=True
    )
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    period: Mapped[Period] = relationship()
    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit_cents >= 0", name="ck_line_debit_nonneg"),
        CheckConstraint("credit_cents >= 0", name="ck_line_credit_nonneg"),
        # Exactly one side carries the amount.
        CheckConstraint("(debit_cents > 0) <> (credit_cents > 0)", name="ck_line_one_side"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    fund_id: Mapped[int] = mapped_column(ForeignKey("funds.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    debit_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    credit_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    memo: Mapped[str] = mapped_column(Text, default="")

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")


class Budget(Base):
    """One budget line per (year, fund, department, account).

    encumbered_cents / actual_cents are mutated only through atomic guarded
    UPDATEs in the service layer; the CHECK constraints are the backstop that
    makes "available never goes negative" a database invariant.
    """

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint("year", "fund_id", "department_id", "account_id", name="uq_budget_line"),
        CheckConstraint("amount_cents >= 0", name="ck_budget_amount_nonneg"),
        CheckConstraint("encumbered_cents >= 0", name="ck_budget_encumbered_nonneg"),
        CheckConstraint("actual_cents >= 0", name="ck_budget_actual_nonneg"),
        CheckConstraint(
            "encumbered_cents + actual_cents <= amount_cents", name="ck_budget_available"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer)
    fund_id: Mapped[int] = mapped_column(ForeignKey("funds.id"))
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    encumbered_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    actual_cents: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    fund: Mapped[Fund] = relationship()
    department: Mapped[Department] = relationship()
    account: Mapped[Account] = relationship()


class ExpenseRequest(Base):
    """Spending request. Status machine:

    pending --approve--> approved --(settled by a posted journal entry)--> posted
    pending/approved --cancel--> cancelled   (encumbrance released exactly once)
    """

    __tablename__ = "expense_requests"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_expense_amount_pos"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'cancelled', 'posted')",
            name="ck_expense_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    # Plain column (no FK) to avoid a create-order cycle with journal_entries.
    journal_entry_id: Mapped[int | None] = mapped_column(Integer)
    created_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
