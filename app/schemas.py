from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class LineIn(BaseModel):
    line_no: int = Field(ge=1)
    fund_code: str = Field(min_length=1, max_length=32)
    department_code: str = Field(min_length=1, max_length=32)
    account_code: str = Field(min_length=1, max_length=32)
    debit_cents: int = Field(default=0, ge=0)
    credit_cents: int = Field(default=0, ge=0)
    reservation_request_no: str | None = Field(default=None, max_length=64)
    description: str = Field(default="", max_length=1000)


class EntryIn(BaseModel):
    voucher_no: str = Field(min_length=1, max_length=64)
    entry_date: date
    period_code: str = Field(min_length=4, max_length=7)
    description: str = Field(default="", max_length=1000)
    lines: list[LineIn] = Field(min_length=2)

    @field_validator("lines")
    @classmethod
    def _unique_line_numbers(cls, v: list[LineIn]) -> list[LineIn]:
        nos = [line.line_no for line in v]
        if len(set(nos)) != len(nos):
            raise ValueError("Duplicate line_no within entry")
        return v


class LineOut(BaseModel):
    line_no: int
    fund_code: str
    department_code: str
    account_code: str
    debit_cents: int
    credit_cents: int
    reservation_request_no: str | None = None
    description: str = ""


class EntryOut(BaseModel):
    id: int
    voucher_no: str
    entry_date: date
    period_code: str
    description: str
    is_reversal: bool
    reverses_voucher_no: str | None = None
    reversal_reason: str | None = None
    created_by: str
    created_at: datetime
    lines: list[LineOut]


class ReversalIn(BaseModel):
    voucher_no: str = Field(min_length=1, max_length=64)
    entry_date: date
    period_code: str = Field(min_length=4, max_length=7)
    reason: str = Field(min_length=1, max_length=1000)
    description: str = Field(default="", max_length=1000)


class BudgetIn(BaseModel):
    year: int = Field(ge=1900, le=9999)
    fund_code: str = Field(min_length=1, max_length=32)
    department_code: str = Field(min_length=1, max_length=32)
    account_code: str = Field(min_length=1, max_length=32)
    amount_cents: int = Field(ge=0)


class ReservationIn(BaseModel):
    request_no: str = Field(min_length=1, max_length=64)
    year: int = Field(ge=1900, le=9999)
    fund_code: str = Field(min_length=1, max_length=32)
    department_code: str = Field(min_length=1, max_length=32)
    account_code: str = Field(min_length=1, max_length=32)
    amount_cents: int = Field(gt=0)
    description: str = Field(default="", max_length=1000)


class ReservationCancelIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ReservationOut(BaseModel):
    id: int
    request_no: str
    year: int
    fund_code: str
    department_code: str
    account_code: str
    amount_cents: int
    status: str
    description: str
    approved_by: str
    approved_at: datetime
    cancelled_at: datetime | None = None
    cancelled_by: str | None = None
    cancel_reason: str | None = None
    consumed_voucher_no: str | None = None
    reversed_voucher_no: str | None = None


class PeriodIn(BaseModel):
    code: str = Field(min_length=4, max_length=7)
    start_date: date
    end_date: date


class PeriodCloseIn(BaseModel):
    reason: str = Field(default="", max_length=1000)


class PeriodReopenIn(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class PeriodOut(BaseModel):
    code: str
    start_date: date
    end_date: date
    is_closed: bool
    closed_reason: str | None = None
    closed_at: datetime | None = None
    closed_by: str | None = None


class FundIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)


class DepartmentIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)


class AccountIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=255)
    account_class: int = Field(ge=1, le=5)
    normal_side: str = Field(min_length=1, max_length=1)

    @field_validator("normal_side")
    @classmethod
    def _side(cls, v: str) -> str:
        v = v.upper()
        if v not in ("D", "C"):
            raise ValueError("normal_side must be 'D' or 'C'")
        return v


class FundOut(BaseModel):
    code: str
    name: str


class DepartmentOut(BaseModel):
    code: str
    name: str


class AccountOut(BaseModel):
    code: str
    name: str
    account_class: int
    normal_side: str


class BudgetOut(BaseModel):
    id: int
    year: int
    fund_code: str
    department_code: str
    account_code: str
    amount_cents: int
    reserved_cents: int
    actual_cents: int
    available_cents: int


class BudgetUsageRow(BaseModel):
    year: int
    fund_code: str
    department_code: str
    account_code: str
    budget_cents: int
    reserved_cents: int
    actual_cents: int
    available_cents: int


class CsvError(BaseModel):
    row: int
    voucher_no: str | None = None
    message: str


class CsvUploadResult(BaseModel):
    posted_entries: list[EntryOut]
    error_count: int
    errors: list[CsvError]
