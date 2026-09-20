"""Idempotent demo data.

Creates a supervisor, several learners, a published 4-step program with
prerequisites, plus enrolments in every interesting state:

* alice  -> CONFIRMED, fully progressed with a certificate
* bob    -> CONFIRMED, partial progress
* carol  -> PENDING seat (waiting for confirmation)
* dave   -> WAITLISTED
* erin   -> CONFIRMED, expired-vs-confirmed demo companion

Re-running the script does not duplicate data.
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.clock import now_utc
from app.database import SessionLocal
from app.models import (
    AuthSession,
    ProgramVersion,
    User,
)
from app.schemas.dto import StepIn
from app.security import hash_password
from app.services import enrollment_service, progress_service, versions_service
from app.statuses import RESULT_PASSED

DEMO_USERS = [
    # login, password, name, role, fixed demo token
    ("sup", "sup-password", "Sasha Supervisor", "supervisor", "demo-token-sup"),
    ("alice", "alice-password", "Alice Able", "learner", "demo-token-alice"),
    ("bob", "bob-password", "Bob Baker", "learner", "demo-token-bob"),
    ("carol", "carol-password", "Carol Chen", "learner", "demo-token-carol"),
    ("dave", "dave-password", "Dave Diaz", "learner", "demo-token-dave"),
]

STEPS = [
    StepIn(title="Onboarding", instruction="Complete orientation module",
           pass_condition="Orientation quiz >= 80%", prerequisite_positions=[]),
    StepIn(title="Safety basics", instruction="Watch safety video",
           pass_condition="Safety acknowledgement signed", prerequisite_positions=[1]),
    StepIn(title="Hands-on drill", instruction="Perform the drill under observation",
           pass_condition="Observer signs off drill", prerequisite_positions=[2]),
    StepIn(title="Final assessment", instruction="Pass the practical assessment",
           pass_condition="Assessor marks all criteria met", prerequisite_positions=[2, 3]),
]


def seed() -> None:
    session = SessionLocal()
    try:
        if session.scalar(select(User).where(User.login == "sup")) is not None:
            print("demo data already present; skipping")
            return

        users: dict[str, User] = {}
        for login, password, name, role, token in DEMO_USERS:
            user = User(
                name=name, login=login, password_hash=hash_password(password), role=role
            )
            session.add(user)
            session.flush()
            session.add(AuthSession(token=token, user_id=user.id))
            users[login] = user

        sup = users["sup"]
        deadline = now_utc() + timedelta(days=14)
        program = versions_service.create_program(
            session,
            supervisor_id=sup.id,
            title="Workplace Safety Certification",
            description="Four-step onboarding and safety track (demo program).",
            capacity=3,
            enrollment_deadline=deadline,
        )
        draft = session.scalar(
            select(ProgramVersion).where(ProgramVersion.program_id == program.id)
        )
        versions_service.replace_steps(
            session, draft.id, STEPS, supervisor_id=sup.id
        )
        versions_service.publish_version(session, draft.id, supervisor_id=sup.id)
        published = session.get(ProgramVersion, draft.id)
        steps = sorted(published.steps, key=lambda s: s.position)

        # alice: confirmed + all 4 steps passed -> certificate issued
        alice_enr = enrollment_service.enroll(session, program.id, users["alice"].id)
        enrollment_service.confirm(session, alice_enr.id, users["alice"].id)
        for step in steps:
            progress_service.submit(
                session,
                enrollment_id=alice_enr.id,
                step_id=step.id,
                learner_id=users["alice"].id,
                content=f"alice completed {step.title}",
            )
            progress_service.evaluate_submission(
                session,
                enrollment_id=alice_enr.id,
                step_id=step.id,
                supervisor_id=sup.id,
                new_status=RESULT_PASSED,
            )

        # bob: confirmed + 1 of 4 steps passed
        bob_enr = enrollment_service.enroll(session, program.id, users["bob"].id)
        enrollment_service.confirm(session, bob_enr.id, users["bob"].id)
        progress_service.submit(
            session,
            enrollment_id=bob_enr.id,
            step_id=steps[0].id,
            learner_id=users["bob"].id,
            content="bob onboarding done",
        )
        progress_service.evaluate_submission(
            session,
            enrollment_id=bob_enr.id,
            step_id=steps[0].id,
            supervisor_id=sup.id,
            new_status=RESULT_PASSED,
        )

        # carol: holds an unconfirmed seat; dave: on the waitlist (capacity=3)
        carol_enr = enrollment_service.enroll(session, program.id, users["carol"].id)
        dave_enr = enrollment_service.enroll(session, program.id, users["dave"].id)

        session.commit()

        print("Seeded demo program:", program.id, "version", published.version_number)
        print("alice enrollment:", alice_enr.id, "(confirmed, certificate issued)")
        print("bob enrollment:", bob_enr.id, "(confirmed, 1 step passed)")
        print("carol enrollment:", carol_enr.id, "status", carol_enr.status)
        print("dave enrollment:", dave_enr.id, "status", dave_enr.status)
        print()
        print("Demo tokens (Authorization: Bearer <token>):")
        for login, _, name, role, token in DEMO_USERS:
            print(f"  {role:10s} {login:6s} -> {token}")
    finally:
        session.close()


if __name__ == "__main__":
    seed()
