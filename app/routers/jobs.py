from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import CurrentUser, require_supervisor
from ..services import enrollments as svc

router = APIRouter(tags=["jobs"])


@router.post("/jobs/expire-seats")
def expire_seats(
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    """Release seats whose 48h confirmation window lapsed and promote the
    waitlist. In production this is triggered by a scheduler; it is exposed
    as an endpoint so it can be driven by a controllable clock in tests."""
    expired = svc.expire_overdue(db)
    return {"expired": expired}
