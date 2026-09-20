from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

# All money is integer cents. 9_999_999_999_999 cents ~= 100 billion currency
# units, comfortably inside BIGINT (int64).
MAX_CENTS = 9_999_999_999_999


class JournalLineIn(BaseModel):
    fund_id: int
    department_id: int
    account_id: int
    debit_cents: int = Field(default=0, ge=0, le=MAX_CENTS)
    credit_cents: int = Field(default=0, ge=0, le=MAX_CENTS)

    @model_validator(mode="after")
    def exactly_one_side(self):
        if (self.debit_cents > 0) == (self.credit_cents > 0):
            raise ValueError("exactly one of debit_cents / credit_cents must be positive")
        return self


class JournalCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    period_id: int
    memo: str = Field(default="", max_length=500)
    encumbrance_id: int | None = None
    lines: list[JournalLineIn] = Field(min_length=2)


class ReversalCreate(BaseModel):
    idempotency_key: str = Field(min_length=1, max_length=128)
    period_id: int
    memo: str = Field(default="", max_length=500)


class JournalLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fund_id: int
    department_id: int
    account_id: int
    debit_cents: int
    credit_cents: int


class JournalEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    idempotency_key: str
    period_id: int
    memo: str
    source: str
    reversal_of_id: int | None
    created_by: str
    created_at: datetime
    lines: list[JournalLineOut]


class BudgetCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    fund_id: int
    department_id: int
    account_id: int
    amount_cents: int = Field(ge=0, le=MAX_CENTS)


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    year: int
    fund_id: int
    department_id: int
    account_id: int
    amount_cents: int
    encumbered_cents: int
    actual_cents: int

    @property
    def available_cents(self) -> int:
        return self.amount_cents - self.encumbered_cents - self.actual_cents


class EncumbranceCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    fund_id: int
    department_id: int
    account_id: int
    amount_cents: int = Field(gt=0, le=MAX_CENTS)
    description: str = Field(default="", max_length=500)


class EncumbranceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    budget_id: int
    amount_cents: int
    status: str
    description: str
    journal_entry_id: int | None
    created_by: str
    created_at: datetime
    closed_at: datetime | None


class PeriodCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    period: int = Field(ge=1, le=12)


class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    year: int
    period: int
    status: str
    closed_by: str | None
    closed_at: datetime | None


class ReopenRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class PeriodAuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    period_id: int
    action: str
    actor: str
    reason: str | None
    created_at: datetime
