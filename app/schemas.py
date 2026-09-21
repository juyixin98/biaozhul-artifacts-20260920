from pydantic import BaseModel, Field, model_validator

# Business cap for a single line/amount, in cents (~10^15). Well within BIGINT.
MAX_CENTS = 999_999_999_999_999


class JournalLineIn(BaseModel):
    fund_code: str = Field(min_length=1, max_length=32)
    department_code: str = Field(min_length=1, max_length=32)
    account_code: str = Field(min_length=1, max_length=32)
    debit_cents: int = Field(default=0, ge=0, le=MAX_CENTS)
    credit_cents: int = Field(default=0, ge=0, le=MAX_CENTS)
    memo: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def exactly_one_side(self):
        if (self.debit_cents > 0) == (self.credit_cents > 0):
            raise ValueError("exactly one of debit_cents / credit_cents must be positive")
        return self


class JournalEntryCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    voucher_no: str | None = Field(default=None, max_length=64)
    memo: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=128)
    expense_request_id: int | None = None
    lines: list[JournalLineIn] = Field(min_length=2)


class ReverseCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)
    memo: str = Field(default="", max_length=500)


class BudgetCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    fund_code: str = Field(min_length=1, max_length=32)
    department_code: str = Field(min_length=1, max_length=32)
    account_code: str = Field(min_length=1, max_length=32)
    amount_cents: int = Field(ge=0, le=MAX_CENTS)


class ExpenseRequestCreate(BaseModel):
    budget_id: int
    amount_cents: int = Field(gt=0, le=MAX_CENTS)
    purpose: str = Field(min_length=1, max_length=500)


class PeriodCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class CloseRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class ReopenRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
