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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Fund(Base):
    __tablename__ = "funds"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Department(Base):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)


class Period(Base):
    """会计期间。status: open / closed。关闭与过账通过行锁串行化。"""

    __tablename__ = "periods"
    __table_args__ = (UniqueConstraint("year", "month", name="uq_period_year_month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")


class JournalEntry(Base):
    """分录（凭证头）。过账即不可变；纠错通过 reversal_of_id 关联的冲销凭证完成。"""

    __tablename__ = "journal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str | None] = mapped_column(String(64))
    period_id: Mapped[int] = mapped_column(ForeignKey("periods.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="posted")
    # 一张凭证最多被冲销一次
    reversal_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("journal_entries.id"), unique=True
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    lines: Mapped[list["JournalLine"]] = relationship(
        back_populates="entry", cascade="all, delete-orphan"
    )
    period: Mapped[Period] = relationship()


class JournalLine(Base):
    __tablename__ = "journal_lines"
    __table_args__ = (
        CheckConstraint("debit_cents >= 0", name="ck_line_debit_nonneg"),
        CheckConstraint("credit_cents >= 0", name="ck_line_credit_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(
        ForeignKey("journal_entries.id"), nullable=False, index=True
    )
    fund_id: Mapped[int] = mapped_column(ForeignKey("funds.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    debit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    credit_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    entry: Mapped[JournalEntry] = relationship(back_populates="lines")


class Budget(Base):
    """按 年度+基金+部门+科目 管理预算；预占/实际在同一行上就地更新（行锁保证并发安全）。"""

    __tablename__ = "budgets"
    __table_args__ = (
        UniqueConstraint(
            "year", "fund_id", "department_id", "account_id", name="uq_budget_scope"
        ),
        CheckConstraint("amount_cents >= 0", name="ck_budget_amount_nonneg"),
        CheckConstraint("encumbered_cents >= 0", name="ck_budget_enc_nonneg"),
        CheckConstraint("actual_cents >= 0", name="ck_budget_act_nonneg"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    fund_id: Mapped[int] = mapped_column(ForeignKey("funds.id"), nullable=False)
    department_id: Mapped[int] = mapped_column(
        ForeignKey("departments.id"), nullable=False
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    encumbered_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_cents: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ExpenditureRequest(Base):
    """支出申请。pending -> approved(预占) -> posted(转实际) 或 -> cancelled(释放预占)。"""

    __tablename__ = "expenditure_requests"
    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_request_amount_pos"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    budget_id: Mapped[int] = mapped_column(ForeignKey("budgets.id"), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

    budget: Mapped[Budget] = relationship()


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
