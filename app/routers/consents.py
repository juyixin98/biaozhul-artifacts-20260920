from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import BatchImportIn, ConsentEventIn
from ..security import get_current_user, require_admin
from ..services import consent as svc
from ..services.consent import ServiceError

router = APIRouter(prefix="/consents", tags=["consents"])


def _run(db: Session, fn):
    try:
        result = fn()
        db.commit()
        return result
    except ServiceError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)
    except Exception:
        db.rollback()
        raise


@router.post("/events", status_code=201)
def record_event(body: ConsentEventIn, db: Session = Depends(get_db),
                 user: User = Depends(require_admin)):
    return _run(db, lambda: svc.apply_event(
        db, org_id=user.org_id, actor=user, **body.model_dump()))


@router.post("/events/batch", status_code=201)
def import_batch(body: BatchImportIn, db: Session = Depends(get_db),
                 user: User = Depends(require_admin)):
    items = [item.model_dump() for item in body.items]
    return _run(db, lambda: {"results": svc.import_batch(
        db, org_id=user.org_id, actor=user, items=items)})


@router.get("/verify")
def verify(subject_ref: str, purpose_code: str, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    try:
        return svc.verify_consent(db, org_id=user.org_id,
                                  subject_ref=subject_ref, purpose_code=purpose_code)
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.get("/history")
def history(subject_ref: str, purpose_code: str, db: Session = Depends(get_db),
            user: User = Depends(get_current_user)):
    try:
        return {"events": svc.event_history(db, org_id=user.org_id,
                                            subject_ref=subject_ref,
                                            purpose_code=purpose_code)}
    except ServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail)


@router.post("/rebuild")
def rebuild(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    return _run(db, lambda: svc.rebuild_states(db, org_id=user.org_id, actor=user))
