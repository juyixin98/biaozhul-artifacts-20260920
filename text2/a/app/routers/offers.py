from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.clock import Clock, get_clock
from app.config import settings
from app.database import get_db
from app.schemas import AssignmentOut, OfferOut, OfferRespond
from app.services import assignment as svc

router = APIRouter(prefix="/offers", tags=["offers"])


@router.post("/{offer_id}/accept")
def accept_offer(offer_id: int, body: OfferRespond,
                 db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    """接受邀请。并发/超时竞争下只保留一个有效分配；重复接受幂等。"""
    offer, assignment = svc.accept_offer(db, offer_id, body.caregiver_id,
                                         clock.now())
    db.commit()
    return {
        "offer": OfferOut.model_validate(offer),
        "assignment": (AssignmentOut.model_validate(assignment)
                       if assignment else None),
    }


@router.post("/{offer_id}/reject")
def reject_offer(offer_id: int, body: OfferRespond,
                 db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    """拒绝邀请并立即尝试重新分配。"""
    offer, new_offer = svc.reject_offer(db, offer_id, body.caregiver_id,
                                        clock.now(), settings)
    db.commit()
    return {
        "offer": OfferOut.model_validate(offer),
        "new_offer_id": new_offer.id if new_offer else None,
    }


@router.post("/expire")
def expire_offers(db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    """超时重排：失效所有过期邀请（8 分钟未接受）并重新分配。"""
    results = svc.expire_offers(db, clock.now(), settings)
    db.commit()
    return {"expired": len(results), "results": results}
