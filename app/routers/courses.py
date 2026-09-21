import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import DomainError
from ..models import Course
from ..schemas import CourseCreate, CourseOut, EnrollmentOut, EnrollRequest
from ..security import CurrentUser, get_current_user, require_supervisor
from ..services import enrollments as enroll_svc
from ..services import programs as program_svc

router = APIRouter(tags=["courses"])


@router.post("/courses", response_model=CourseOut, status_code=201)
def create_course(
    body: CourseCreate,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(require_supervisor),
):
    program_svc.get_program(db, body.program_id)  # 404 if unknown
    course = Course(
        program_id=body.program_id,
        title=body.title,
        capacity=body.capacity,
        enrollment_deadline=body.enrollment_deadline,
    )
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


@router.get("/courses/{course_id}", response_model=CourseOut)
def get_course(course_id: uuid.UUID, db: Session = Depends(get_db)):
    course = db.get(Course, course_id)
    if course is None:
        raise DomainError(404, "course not found")
    return course


@router.post("/courses/{course_id}/enrollments", response_model=EnrollmentOut, status_code=201)
def enroll(
    course_id: uuid.UUID,
    body: EnrollRequest,
    db: Session = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    if body.learner_id != user.user_id and not user.is_supervisor:
        raise DomainError(403, "learners can only enroll themselves")
    return enroll_svc.enroll(db, course_id, body.learner_id, body.version_id)
