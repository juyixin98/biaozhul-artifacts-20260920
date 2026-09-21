"""Idempotent demo data.

Creates:
* supervisor Alice and learners Bob / Carol / Dan
* program "New Trainer Onboarding" v1 (published, 4 ordered steps) and
  v2 (published, 5 steps) — program points at v2, demonstrating rollback
* a capacity-2 course on v2 (deadline +7d), plus a completed v1 demo
  enrolment for Bob with a VALID certificate on v1 — proving a rollback/
  republish never changes already-started learning content.

Run: ``python -m app.seed`` (after migrations).
Safe to run repeatedly.
"""
from datetime import timedelta

from sqlalchemy import select

from app import clock as clock_svc
from app.database import SessionLocal
from app.models import (
    Course,
    Program,
    User,
    ROLE_LEARNER,
    ROLE_SUPERVISOR,
)
from app.services import enrollments as enroll_svc
from app.services import progress as progress_svc
from app.services import programs as program_svc

V1_STEPS = [
    {
        "key": "intro",
        "position": 1,
        "instruction": "Read the welcome pack and introduce yourself.",
        "pass_condition": "Intro posted in the group channel.",
        "prerequisite_keys": [],
    },
    {
        "key": "safety",
        "position": 2,
        "instruction": "Complete the workshop safety walkthrough.",
        "pass_condition": "Safety quiz score >= 90%.",
        "prerequisite_keys": ["intro"],
    },
    {
        "key": "demo",
        "position": 3,
        "instruction": "Deliver a 10-minute demo training segment.",
        "pass_condition": "Supervisor marks the demo rubric all green.",
        "prerequisite_keys": ["safety"],
    },
    {
        "key": "signoff",
        "position": 4,
        "instruction": "Final sign-off conversation with the lead trainer.",
        "pass_condition": "Lead trainer signs the sign-off form.",
        "prerequisite_keys": ["demo"],
    },
]

V2_STEPS = V1_STEPS[:3] + [
    {
        "key": "first_class",
        "position": 4,
        "instruction": "Run your first real class with a mentor observing.",
        "pass_condition": "Mentor report submitted with no critical findings.",
        "prerequisite_keys": ["demo"],
    },
    {
        "key": "signoff",
        "position": 5,
        "instruction": "Final sign-off conversation with the lead trainer.",
        "pass_condition": "Lead trainer signs the sign-off form.",
        "prerequisite_keys": ["first_class"],
    },
]


def _get_or_create_user(db, name: str, role: str) -> User:
    user = db.scalar(select(User).where(User.name == name))
    if user is None:
        user = User(name=name, role=role)
        db.add(user)
        db.flush()
    return user


def run() -> None:
    db = SessionLocal()
    try:
        alice = _get_or_create_user(db, "Alice Supervisor", ROLE_SUPERVISOR)
        bob = _get_or_create_user(db, "Bob Learner", ROLE_LEARNER)
        carol = _get_or_create_user(db, "Carol Learner", ROLE_LEARNER)
        dan = _get_or_create_user(db, "Dan Learner", ROLE_LEARNER)
        db.flush()

        program = db.scalar(select(Program).where(Program.title == "New Trainer Onboarding"))
        if program is None:
            program = program_svc.create_program(
                db,
                supervisor_id=alice.id,
                title="New Trainer Onboarding",
                description="Onboarding program for new workshop trainers.",
            )
            db.flush()

        if not program.versions:
            v1 = program_svc.create_version(
                db,
                supervisor_id=alice.id,
                program_id=program.id,
                steps=V1_STEPS,
                publish=True,
            )
            db.flush()

            # Bob completes v1 and earns a certificate on that pinned version.
            course_v1 = Course(
                title="Onboarding Cohort v1 (archived)",
                version_id=v1.id,
                capacity=10,
                enroll_deadline=(clock_svc.now(db) + timedelta(days=30)).replace(
                    tzinfo=None
                ),
            )
            db.add(course_v1)
            db.flush()

            bob_enrollment = enroll_svc.enroll(
                db, course_id=course_v1.id, learner_id=bob.id
            )
            enroll_svc.confirm(db, course_id=course_v1.id, learner_id=bob.id)
            db.flush()
            for step in V1_STEPS:
                progress_svc.submit_result(
                    db,
                    enrollment_id=bob_enrollment.id,
                    learner_id=bob.id,
                    step_key=step["key"],
                    content=f"Bob's work for {step['key']}",
                )
                db.flush()
                progress_svc.review_result(
                    db,
                    enrollment_id=bob_enrollment.id,
                    step_key=step["key"],
                    passed=True,
                )
                db.flush()
            db.flush()

            # v2 is published after Bob's cohort started -> Bob stays on v1.
            v2 = program_svc.create_version(
                db,
                supervisor_id=alice.id,
                program_id=program.id,
                steps=V2_STEPS,
                publish=True,
            )
            db.flush()

            deadline = clock_svc.now(db) + timedelta(days=7)
            current_course = Course(
                title="Onboarding Cohort v2",
                version_id=v2.id,
                capacity=2,
                enroll_deadline=deadline.replace(tzinfo=None),
            )
            db.add(current_course)
            db.flush()

            # Two seats filled by Carol and Dan (confirmed / offered).
            for learner in (carol, dan):
                enroll_svc.enroll(db, course_id=current_course.id, learner_id=learner.id)
            enroll_svc.confirm(db, course_id=current_course.id, learner_id=carol.id)
            db.flush()

        db.commit()

        program_id = program.id
        current_course = db.scalar(
            select(Course)
            .where(Course.title == "Onboarding Cohort v2")
        )
        print("Seed complete.")
        print(f"  Supervisor Alice:  X-User-Id {alice.id}")
        print(f"  Learners Bob/Carol/Dan: X-User-Id {bob.id} / {carol.id} / {dan.id}")
        print(f"  Program id: {program_id}")
        print(f"  Current v2 course id (capacity 2): {current_course.id if current_course else 'n/a'}")
        print("  Bob holds a valid certificate pinned to v1; v2 is the current version.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
