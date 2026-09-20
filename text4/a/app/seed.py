"""Idempotent demo data.

Creates one manager, three learners, a three-step program (published v1),
an unpublished v2, and a capacity-2 course so the waitlist is one sign-up
away. Run via ``python -m app.seed`` or automatically in Docker when
``SEED_DEMO=1``.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from .clock import utcnow
from .database import SessionLocal
from .models import (
    Course,
    Program,
    User,
    UserRole,
)
from .schemas import ProgramCreate, StepIn, VersionCreate, CourseCreate
from .services import enrollments as enrollment_service
from .services import programs as program_service


def seed() -> None:
    db = SessionLocal()
    try:
        if db.scalar(select(User.id).where(User.email == "manager@skillpulse.test")):
            print("demo data already present; skipping")
            return

        now = utcnow(db)

        manager = User(
            email="manager@skillpulse.test",
            name="Maya Manager",
            role=UserRole.MANAGER,
            created_at=now,
        )
        learners = [
            User(email=f"learner{i}@skillpulse.test", name=f"Learner {i}",
                 role=UserRole.LEARNER, created_at=now)
            for i in range(1, 4)
        ]
        db.add_all([manager, *learners])
        db.flush()

        program = program_service.create_program(
            db,
            manager,
            ProgramCreate(title="Onboarding Fundamentals", description="Demo program"),
        )

        v1 = program_service.create_version(
            db,
            manager,
            program.id,
            VersionCreate(
                steps=[
                    StepIn(
                        title="Safety briefing",
                        description="Watch the safety video.",
                        pass_criteria="Quiz score >= 80%",
                    ),
                    StepIn(
                        title="Hands-on drill",
                        description="Complete the guided drill.",
                        pass_criteria="Instructor sign-off",
                        prerequisite_orders=[1],
                    ),
                    StepIn(
                        title="Final review",
                        description="Review with your lead.",
                        pass_criteria="Lead approval",
                        prerequisite_orders=[1, 2],
                    ),
                ]
            ),
        )
        program_service.publish_version(db, manager, v1.id)

        # A newer draft: shows that edits create new versions, never mutate v1.
        program_service.create_version(
            db,
            manager,
            program.id,
            VersionCreate(
                steps=[
                    StepIn(title="Safety briefing v2", pass_criteria="Quiz >= 90%"),
                    StepIn(
                        title="Hands-on drill v2",
                        pass_criteria="Instructor sign-off",
                        prerequisite_orders=[1],
                    ),
                ]
            ),
        )

        course = enrollment_service.create_course(
            db,
            manager,
            CourseCreate(
                version_id=v1.id,
                capacity=2,
                enrollment_deadline=now + timedelta(days=30),
            ),
        )

        # Two learners take the seats; the third is one call away from queueing.
        enrollment_service.enroll(db, learners[0], course.id)
        enrollment_service.enroll(db, learners[1], course.id)

        print("demo data created")
        print(f"  manager   X-User-Id={manager.id}  manager@skillpulse.test")
        for learner in learners:
            print(f"  learner   X-User-Id={learner.id}  {learner.email}")
        print(f"  program   id={program.id}  published version id={v1.id}")
        print(f"  course    id={course.id}  capacity=2 (2/2 seats held)")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
