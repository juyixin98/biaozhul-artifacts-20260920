"""Idempotent demo-data seeder.

Run with:  python -m app.seed
Creates a supervisor-authored program (published), a course with capacity 2,
and a few learners in different enrollment states. Safe to run repeatedly.
"""
from datetime import timedelta

from sqlalchemy import select

from . import clock
from .db import SessionLocal
from .models import Course, Enrollment, Program
from .schemas import StepIn
from .services import enrollments as enroll_svc
from .services import programs as program_svc

DEMO_PROGRAM_TITLE = "SkillPulse 安全作业培训"


def seed() -> None:
    db = SessionLocal()
    try:
        if db.scalar(select(Program).where(Program.title == DEMO_PROGRAM_TITLE)):
            print("demo data already present, skipping")
            return

        steps = [
            StepIn(step_key="safety-video", title="观看安全视频",
                   instructions="完整观看 15 分钟安全视频",
                   pass_condition="观看进度达到 100%"),
            StepIn(step_key="quiz", title="安全知识测验",
                   instructions="完成 20 道选择题",
                   pass_condition="得分 >= 80",
                   prerequisites=["safety-video"]),
            StepIn(step_key="drill", title="现场演练",
                   instructions="参加线下消防演练",
                   pass_condition="教官现场签字确认",
                   prerequisites=["quiz"]),
        ]
        program = program_svc.create_program(
            db, "supervisor-demo", DEMO_PROGRAM_TITLE,
            "新员工安全作业必修培训", steps, "初始版本",
        )
        version = program.versions[0]
        program_svc.publish_version(db, version.id)

        course = Course(
            program_id=program.id,
            title="2026 年 9 月安全培训班",
            capacity=2,
            enrollment_deadline=clock.now() + timedelta(days=30),
        )
        db.add(course)
        db.commit()
        db.refresh(course)

        # learner-a takes a seat and confirms; learner-b takes the last seat
        # (pending); learner-c lands on the waitlist.
        e1 = enroll_svc.enroll(db, course.id, "learner-a")
        enroll_svc.confirm(db, e1.id, "learner-a")
        enroll_svc.enroll(db, course.id, "learner-b")
        e3 = enroll_svc.enroll(db, course.id, "learner-c")
        assert e3.status == "waitlisted"

        print(f"seeded program={program.id} version={version.id} course={course.id}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
