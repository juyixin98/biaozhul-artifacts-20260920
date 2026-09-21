from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import CurrentUser, require_learner, require_supervisor
from app.models import Enrollment
from app.schemas import CourseCreate
from app.services import enrollments as svc

router = APIRouter(tags=["courses"])


@router.post("/courses", status_code=201)
def create_course(
    payload: CourseCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    course = svc.create_course(
        db,
        title=payload.title,
        version_id=payload.version_id,
        capacity=payload.capacity,
        enroll_deadline=payload.enroll_deadline,
    )
    db.commit()
    return svc.course_view(db, course.id)


@router.get("/courses")
def list_courses(db: Session = Depends(get_db)):
    from app.models import Course

    return [
        svc.course_view(db, course.id)
        for course in db.scalars(select(Course).order_by(Course.id)).all()
    ]


@router.get("/courses/{course_id}")
def get_course(course_id: int, db: Session = Depends(get_db)):
    return svc.course_view(db, course_id)


# ---------------------------------------------------------------------------
# Enrolment (learner acts on their own behalf)
# ---------------------------------------------------------------------------

@router.post("/courses/{course_id}/enroll", status_code=201)
def enroll(
    course_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_learner),
):
    enrollment = svc.enroll(db, course_id=course_id, learner_id=user.id)
    db.commit()
    db.refresh(enrollment)
    return enrollment.to_dict()


@router.post("/courses/{course_id}/confirm")
def confirm(
    course_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_learner),
):
    enrollment = svc.confirm(db, course_id=course_id, learner_id=user.id)
    db.commit()
    db.refresh(enrollment)
    return enrollment.to_dict()


@router.post("/courses/{course_id}/cancel")
def cancel(
    course_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_learner),
):
    enrollment = svc.cancel(db, course_id=course_id, learner_id=user.id)
    db.commit()
    db.refresh(enrollment)
    return enrollment.to_dict()


@router.get("/courses/{course_id}/my-enrollment")
def my_enrollment(
    course_id: int,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_learner),
):
    enrollment = db.scalar(
        select(Enrollment).where(
            Enrollment.course_id == course_id,
            Enrollment.learner_id == user.id,
        )
    )
    if enrollment is None:
        return None
    data = enrollment.to_dict()
    if enrollment.status == "waitlisted":
        data["queue_position"] = svc.waitlist_position(
            db, course_id=course_id, learner_id=user.id
        )
    return data
