from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import Alert, DepartmentMembership, Organization, User, UserRole


def _escape_like(path: str) -> str:
    return path.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def alert_visibility_condition(viewer: User, db: Session):
    """SQLAlchemy condition on Alert.user_id for list/detail queries.

    - admin: everything
    - manager: direct reports only (not the whole subtree)
    - analyst: users whose organization is within an authorized department subtree
    - others: nothing
    """
    if viewer.role is UserRole.admin:
        return None
    if viewer.role is UserRole.manager:
        return Alert.user_id.in_(select(User.id).where(User.manager_id == viewer.id))
    if viewer.role is UserRole.analyst:
        dept_ids = [
            r[0]
            for r in db.execute(
                select(DepartmentMembership.department_id).where(
                    DepartmentMembership.user_id == viewer.id
                )
            ).all()
        ]
        if not dept_ids:
            return Alert.user_id.in_(select(User.id).where(User.id == -1))
        paths = [
            r[0]
            for r in db.execute(
                select(Organization.path).where(Organization.id.in_(dept_ids))
            ).all()
        ]
        predicates = [
            Alert.user_id.in_(
                select(User.id)
                .join(Organization, User.org_id == Organization.id)
                .where(Organization.path.like(_escape_like(p) + "%", escape="\\"))
            )
            for p in paths
        ]
        return or_(*predicates)
    return Alert.user_id.in_(select(User.id).where(User.id == -1))


def can_view_alert(db: Session, viewer: User, alert: Alert) -> bool:
    target = db.get(User, alert.user_id)
    return target is not None and can_view_user(db, viewer, target)


def ensure_can_view_alert(db: Session, viewer: User, alert: Alert) -> None:
    if not can_view_alert(db, viewer, alert):
        # 404 (not 403) so existence is not leaked across scope boundaries.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found")


def can_view_user(db: Session, viewer: User, target: User) -> bool:
    if viewer.role is UserRole.admin:
        return True
    if viewer.role is UserRole.manager:
        return target.manager_id == viewer.id
    if viewer.role is UserRole.analyst:
        if target.org_id is None:
            return False
        dept_ids = {
            r[0]
            for r in db.execute(
                select(DepartmentMembership.department_id).where(
                    DepartmentMembership.user_id == viewer.id
                )
            ).all()
        }
        paths = {
            r[0]
            for r in db.execute(select(Organization.path).where(Organization.id.in_(dept_ids))).all()
        }
        target_org = db.get(Organization, target.org_id)
        return target_org is not None and any(target_org.path.startswith(p) for p in paths)
    return False
