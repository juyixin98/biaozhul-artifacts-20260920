from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_user, scoped_employees_query
from app.models import BaselineVersion, Employee, User
from app.schemas import BaselineOut

router = APIRouter(prefix="/api/baselines", tags=["baselines"])


@router.get("", response_model=list[BaselineOut])
def list_baselines(
    employee_id: int | None = Query(default=None),
    limit: int = Query(default=100, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Scope baselines to the employees the user is allowed to see.
    visible = scoped_employees_query(db, user).with_entities(Employee.id)
    q = db.query(BaselineVersion).filter(BaselineVersion.employee_id.in_(visible))
    if employee_id is not None:
        q = q.filter(BaselineVersion.employee_id == employee_id)
    return q.order_by(BaselineVersion.id).limit(limit).offset(offset).all()
