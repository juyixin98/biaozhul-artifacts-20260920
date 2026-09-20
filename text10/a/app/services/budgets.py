from sqlalchemy import select

from ..errors import DomainError
from ..models import Budget, Encumbrance, utcnow


def _available(budget: Budget) -> int:
    return budget.amount_cents - budget.encumbered_cents - budget.actual_cents


def approve_encumbrance(
    db,
    *,
    year: int,
    fund_id: int,
    department_id: int,
    account_id: int,
    amount_cents: int,
    description: str,
    actor: str,
) -> Encumbrance:
    """Reserve budget for a spending request.

    The budget row is locked FOR UPDATE, so concurrent approvals serialize
    and can never push available below zero.
    """
    budget = db.scalar(
        select(Budget)
        .where(
            Budget.year == year,
            Budget.fund_id == fund_id,
            Budget.department_id == department_id,
            Budget.account_id == account_id,
        )
        .with_for_update()
    )
    if budget is None:
        raise DomainError(
            404,
            "budget-not-found",
            "no budget for the given year/fund/department/account",
        )
    if _available(budget) < amount_cents:
        raise DomainError(
            409,
            "budget-exceeded",
            f"available {_available(budget)} cents < requested {amount_cents} cents",
        )
    budget.encumbered_cents += amount_cents
    encumbrance = Encumbrance(
        budget_id=budget.id,
        amount_cents=amount_cents,
        description=description,
        created_by=actor,
    )
    db.add(encumbrance)
    db.flush()
    return encumbrance


def cancel_encumbrance(db, encumbrance_id: int, actor: str) -> Encumbrance:
    """Release an open encumbrance. Releases exactly once."""
    encumbrance = db.scalar(
        select(Encumbrance).where(Encumbrance.id == encumbrance_id).with_for_update()
    )
    if encumbrance is None:
        raise DomainError(404, "encumbrance-not-found", f"encumbrance {encumbrance_id} does not exist")
    if encumbrance.status != "open":
        raise DomainError(
            409,
            "encumbrance-not-open",
            f"encumbrance {encumbrance_id} is already {encumbrance.status}",
        )
    budget = db.scalar(
        select(Budget).where(Budget.id == encumbrance.budget_id).with_for_update()
    )
    budget.encumbered_cents -= encumbrance.amount_cents
    encumbrance.status = "cancelled"
    encumbrance.closed_at = utcnow()
    db.flush()
    return encumbrance
