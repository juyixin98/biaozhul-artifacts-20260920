from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Account, Department, Fund
from ..schemas import AccountIn, AccountOut, DepartmentIn, DepartmentOut, FundIn, FundOut
from ..security import Principal, require_roles

router = APIRouter(tags=["master-data"])

_READERS = require_roles("lead", "accountant", "auditor")
_LEAD = require_roles("lead")


@router.put("/funds/{code}", response_model=FundOut)
def put_fund(
    code: str,
    payload: FundIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_LEAD),
):
    if payload.code != code:
        from ..errors import ValidationError

        raise ValidationError("Fund code in path and body must match")
    fund = db.get(Fund, code)
    if fund is None:
        fund = Fund(code=payload.code, name=payload.name)
        db.add(fund)
    else:
        fund.name = payload.name
    db.commit()
    return {"code": fund.code, "name": fund.name}


@router.get("/funds", response_model=list[FundOut])
def get_funds(db: Session = Depends(get_db), principal: Principal = Depends(_READERS)):
    return [{"code": f.code, "name": f.name} for f in db.scalars(select(Fund).order_by(Fund.code))]


@router.put("/departments/{code}", response_model=DepartmentOut)
def put_department(
    code: str,
    payload: DepartmentIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_LEAD),
):
    if payload.code != code:
        from ..errors import ValidationError

        raise ValidationError("Department code in path and body must match")
    dept = db.get(Department, code)
    if dept is None:
        dept = Department(code=payload.code, name=payload.name)
        db.add(dept)
    else:
        dept.name = payload.name
    db.commit()
    return {"code": dept.code, "name": dept.name}


@router.get("/departments", response_model=list[DepartmentOut])
def get_departments(
    db: Session = Depends(get_db), principal: Principal = Depends(_READERS)
):
    return [
        {"code": d.code, "name": d.name}
        for d in db.scalars(select(Department).order_by(Department.code))
    ]


@router.put("/accounts/{code}", response_model=AccountOut)
def put_account(
    code: str,
    payload: AccountIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(_LEAD),
):
    if payload.code != code:
        from ..errors import ValidationError

        raise ValidationError("Account code in path and body must match")
    account = db.get(Account, code)
    if account is None:
        account = Account(
            code=payload.code,
            name=payload.name,
            account_class=payload.account_class,
            normal_side=payload.normal_side,
        )
        db.add(account)
    else:
        account.name = payload.name
        account.account_class = payload.account_class
        account.normal_side = payload.normal_side
    db.commit()
    return {
        "code": account.code,
        "name": account.name,
        "account_class": account.account_class,
        "normal_side": account.normal_side,
    }


@router.get("/accounts", response_model=list[AccountOut])
def get_accounts(
    db: Session = Depends(get_db), principal: Principal = Depends(_READERS)
):
    return [
        {
            "code": a.code,
            "name": a.name,
            "account_class": a.account_class,
            "normal_side": a.normal_side,
        }
        for a in db.scalars(select(Account).order_by(Account.code))
    ]
