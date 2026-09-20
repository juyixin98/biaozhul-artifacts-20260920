from typing import Literal

from pydantic import BaseModel, Field


class JournalLineIn(BaseModel):
    fund_id: int
    department_id: int
    account_id: int
    debit_cents: int = Field(default=0, ge=0)
    credit_cents: int = Field(default=0, ge=0)


class JournalCreateIn(BaseModel):
    period_id: int
    description: str = ""
    lines: list[JournalLineIn] = Field(min_length=2)


class JournalLineOut(BaseModel):
    id: int
    fund_id: int
    department_id: int
    account_id: int
    debit_cents: int
    credit_cents: int

    model_config = {"from_attributes": True}


class JournalEntryOut(BaseModel):
    id: int
    period_id: int
    description: str
    status: str
    reversal_of_id: int | None
    created_by: str
    lines: list[JournalLineOut]

    model_config = {"from_attributes": True}


class PostResult(BaseModel):
    entry: JournalEntryOut
    idempotent_replay: bool = False


class BudgetCreateIn(BaseModel):
    year: int
    fund_id: int
    department_id: int
    account_id: int
    amount_cents: int = Field(ge=0)


class BudgetOut(BaseModel):
    id: int
    year: int
    fund_id: int
    department_id: int
    account_id: int
    amount_cents: int
    encumbered_cents: int
    actual_cents: int
    available_cents: int


class ExpenditureRequestIn(BaseModel):
    budget_id: int
    amount_cents: int = Field(gt=0)
    purpose: str = ""


class ExpenditureRequestOut(BaseModel):
    id: int
    budget_id: int
    amount_cents: int
    purpose: str
    status: str
    created_by: str

    model_config = {"from_attributes": True}


class PeriodCreateIn(BaseModel):
    year: int
    month: int = Field(ge=1, le=12)


class PeriodOut(BaseModel):
    id: int
    year: int
    month: int
    status: str

    model_config = {"from_attributes": True}


class ReopenIn(BaseModel):
    reason: str = Field(min_length=5, max_length=1000)


class AuditLogOut(BaseModel):
    id: int
    actor: str
    action: str
    entity_type: str
    entity_id: str
    reason: str
    detail: str
    created_at: str


class BudgetReportRow(BaseModel):
    budget_id: int
    year: int
    fund_code: str
    department_code: str
    account_code: str
    amount_cents: int
    encumbered_cents: int
    actual_cents: int
    available_cents: int


class ImportResult(BaseModel):
    vouchers: int
    entry_ids: list[int]
