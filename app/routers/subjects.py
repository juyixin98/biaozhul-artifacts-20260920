from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..security import get_current_user, require_admin
from ..services import consent as svc
from ..services.consent import ServiceError

router = APIRouter(prefix="/subjects", tags=["subjects"])


@router.post("/{subject_ref}/exports", status_code=201)
def create_export(subject_ref: str, db: Session = Depends(get_db),
                  user: User = Depends(require_admin)):
    try:
        result = svc.create_export(db, org_id=user.org_id, actor=user,
                                   subject_ref=subject_ref)
        db.commit()
        return result
    except ServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/{subject_ref}/exports")
def list_exports(subject_ref: str, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    try:
        return {"exports": svc.list_exports(db, org_id=user.org_id,
                                            subject_ref=subject_ref)}
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.delete("/{subject_ref}")
def remove_subject(subject_ref: str, db: Session = Depends(get_db),
                   user: User = Depends(require_admin)):
    try:
        result = svc.delete_subject(db, org_id=user.org_id, actor=user,
                                    subject_ref=subject_ref)
        db.commit()
        return result
    except ServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
