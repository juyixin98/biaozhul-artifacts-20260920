"""Seed a small demo dataset: one unit, admin + scoped coordinator, workers
with qualifications, and a care plan whose tasks are generated immediately.

Usage:
    python -m scripts.seed
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.clock import Clock
from app.db import SessionLocal
from app.models import (
    CarePlan,
    Coordinator,
    CoordinatorUnitGrant,
    Qualification,
    Unit,
    Worker,
    WorkerQualification,
)
from app.services.generation import create_plan

CNA = "CNA"          # certified nursing assistant
LIFT = "LIFT_TEAM"   # two-person lift trained


def run() -> None:
    clock = Clock()
    now = clock.now()
    with SessionLocal() as db:
        unit = db.scalar(select(Unit).where(Unit.name == "North District"))
        if unit is None:
            unit = Unit(name="North District")
            db.add(unit)
            db.flush()

        admin = db.scalar(select(Coordinator).where(Coordinator.name == "Admin Lin"))
        if admin is None:
            admin = Coordinator(name="Admin Lin", is_admin=True)
            db.add(admin)

        scoped = db.scalar(select(Coordinator).where(Coordinator.name == "Coordinator Wang"))
        if scoped is None:
            scoped = Coordinator(name="Coordinator Wang", is_admin=False)
            db.add(scoped)
            db.flush()
            db.add(CoordinatorUnitGrant(coordinator_id=scoped.id, unit_id=unit.id))

        for code, name in [(CNA, "Certified Nursing Assistant"), (LIFT, "Lift Team Trained")]:
            if db.scalar(select(Qualification).where(Qualification.code == code)) is None:
                db.add(Qualification(code=code, name=name))
        db.flush()

        workers = []
        for i, name in enumerate(["Anna", "Bailey", "Chen"], start=1):
            w = db.scalar(select(Worker).where(Worker.name == name))
            if w is None:
                w = Worker(name=name, unit_id=unit.id, active=True)
                db.add(w)
                db.flush()
            workers.append(w)

        for w, codes in ((workers[0], [CNA, LIFT]), (workers[1], [CNA]), (workers[2], [CNA])):
            for code in codes:
                qual = db.scalar(select(Qualification).where(Qualification.code == code))
                exists = db.scalar(
                    select(WorkerQualification).where(
                        WorkerQualification.worker_id == w.id,
                        WorkerQualification.qualification_id == qual.id,
                    )
                )
                if exists is None:
                    db.add(WorkerQualification(
                        worker_id=w.id, qualification_id=qual.id,
                        valid_from=now - timedelta(days=30), valid_until=None,
                    ))

        if db.scalar(select(CarePlan).where(CarePlan.external_id == "DEMO-001")) is None:
            create_plan(
                db,
                external_id="DEMO-001",
                client_name="Mrs. Zhang",
                unit_id=unit.id,
                timezone="Asia/Shanghai",
                templates=[
                    {
                        "code": "MORNING_CARE",
                        "name": "Morning personal care",
                        "window_start_minute": 8 * 60,
                        "window_end_minute": 9 * 60,
                        "duration_minutes": 60,
                        "weekday_mask": [],
                        "qualification_codes": [CNA],
                        "prerequisite_codes": [],
                    },
                    {
                        "code": "LIFT_TRANSFER",
                        "name": "Lift-assisted transfer",
                        "window_start_minute": 9 * 60 + 30,
                        "window_end_minute": 11 * 60,
                        "duration_minutes": 30,
                        "weekday_mask": [1, 3, 5],
                        "qualification_codes": [CNA, LIFT],
                        "prerequisite_codes": ["MORNING_CARE"],
                    },
                ],
                now=now,
                actor_id="seed",
            )

        db.commit()
        print("seed complete: unit=%r coordinators=%r workers=%r plan=DEMO-001"
              % (unit.name, [admin and admin.name, scoped and scoped.name], [w.name for w in workers]))


if __name__ == "__main__":
    run()
