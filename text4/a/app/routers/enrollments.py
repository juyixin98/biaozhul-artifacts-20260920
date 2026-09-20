from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_learner, current_manager
from ..models import User
from ..schemas import CourseCreate, CourseOut, EnrollmentOut
from ..serializers import course_out, enrollment_out
from ..services import enrollments as service

router = APIRouter(tags=["courses"])


@router.post("/courses", response_model=CourseOut, status_code=201)
def create_course(
    payload: CourseCreate,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> CourseOut:
    return course_out(db, service.create_course(db, manager, payload))


@router.get("/courses/{course_id}", response_model=CourseOut)
def get_course(course_id: int, db: Session = Depends(get_db)) -> CourseOut:
    return course_out(db, service.get_course(db, course_id))


@router.post("/courses/{course_id}/enroll", response_model=EnrollmentOut, status_code=201)
def enroll(
    course_id: int,
    db: Session = Depends(get_db),
    learner: User = Depends(current_learner),
) -> EnrollmentOut:
    return enrollment_out(service.enroll(db, learner, course_id))


@router.post("/enrollments/{enrollment_id}/confirm", response_model=EnrollmentOut)
def confirm(
    enrollment_id: int,
    db: Session = Depends(get_db),
    learner: User = Depends(current_learner),
) -> EnrollmentOut:
    return enrollment_out(service.confirm(db, learner, enrollment_id))


@router.post("/enrollments/{enrollment_id}/cancel", response_model=EnrollmentOut)
def cancel(
    enrollment_id: int,
    db: Session = Depends(get_db),
    learner: User = Depends(current_learner),
) -> EnrollmentOut:
    return enrollment_out(service.cancel(db, learner, enrollment_id))


@router.get("/enrollments/{enrollment_id}", response_model=EnrollmentOut)
def get_enrollment(
    enrollment_id: int,
    db: Session = Depends(get_db),
    learner: User = Depends(current_learner),
) -> EnrollmentOut:
    return enrollment_out(service.get_owned_enrollment(db, learner, enrollment_id))


@router.post("/courses/{course_id}/expire-holds", response_model=dict)
def expire_holds(
    course_id: int,
    db: Session = Depends(get_db),
    manager: User = Depends(current_manager),
) -> dict:
    """Manually run the 48h-hold sweep (also safe to call on a schedule)."""
    expired = service.expire_due_seats(db, course_id)
    return {"expired": expired}
