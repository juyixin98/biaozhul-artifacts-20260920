import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AnalystDepartment, Alert, Employee, User
from app.security import decode_access_token

bearer = HTTPBearer(auto_error=True)


def get_current_user(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    try:
        claims = decode_access_token(creds.credentials)
        user_id = int(claims["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or expired token"
        )
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unknown user")
    return user


def scoped_alerts_query(db: Session, user: User):
    """Alerts visible to this user.

    - admin:   everything
    - manager: alerts of their direct reports only
    - analyst: alerts of employees in their authorized departments
    """
    q = db.query(Alert).join(Employee, Alert.employee_id == Employee.id)
    if user.role == "admin":
        return q
    if user.role == "manager":
        return q.filter(Employee.manager_id == user.employee_id)
    if user.role == "analyst":
        dept_ids = db.query(AnalystDepartment.department_id).filter(
            AnalystDepartment.user_id == user.id
        )
        return q.filter(Employee.department_id.in_(dept_ids))
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="unsupported role")


def scoped_employees_query(db: Session, user: User):
    """Employees visible to this user (same rules as alert scoping)."""
    q = db.query(Employee)
    if user.role == "admin":
        return q
    if user.role == "manager":
        return q.filter(Employee.manager_id == user.employee_id)
    if user.role == "analyst":
        dept_ids = db.query(AnalystDepartment.department_id).filter(
            AnalystDepartment.user_id == user.id
        )
        return q.filter(Employee.department_id.in_(dept_ids))
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="unsupported role")


def get_visible_alert_or_403(db: Session, user: User, alert_id: int) -> Alert:
    alert = db.get(Alert, alert_id)
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="alert not found")
    visible = (
        scoped_alerts_query(db, user).filter(Alert.id == alert_id).first() is not None
    )
    if not visible:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="alert outside your scope"
        )
    return alert
