"""Idempotent sample data for trying the API locally.

Creates two units, two coordinators (one authorised per unit), three workers
with qualifications, and two linked daily care plans (bathing -> dressing),
then generates the next 14 days of tasks.

Run:  python -m app.seed
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.clock import clock
from app.database import SessionLocal
from app.models import (
    CarePlan,
    CareWorker,
    Coordinator,
    Qualification,
    Recurrence,
    Unit,
)
from app.schemas import PlanIn
from app.services import plans as plans_service


def _get_or_create_unit(db, name: str) -> Unit:
    unit = db.scalars(select(Unit).where(Unit.name == name)).first()
    if unit is None:
        unit = Unit(name=name)
        db.add(unit)
        db.flush()
    return unit


def _get_or_create_coordinator(db, name: str, unit: Unit) -> Coordinator:
    coordinator = db.scalars(select(Coordinator).where(Coordinator.name == name)).first()
    if coordinator is None:
        coordinator = Coordinator(name=name, units=[unit], created_at=clock.now())
        db.add(coordinator)
        db.flush()
    return coordinator


def _get_or_create_worker(db, name: str, unit: Unit, tz: str) -> CareWorker:
    worker = db.scalars(select(CareWorker).where(CareWorker.name == name)).first()
    if worker is None:
        worker = CareWorker(
            name=name, unit_id=unit.id, timezone=tz, active=True, created_at=clock.now()
        )
        db.add(worker)
        db.flush()
    return worker


def main() -> None:
    db = SessionLocal()
    try:
        north = _get_or_create_unit(db, "North District")
        south = _get_or_create_unit(db, "South District")
        db.flush()

        coord_north = _get_or_create_coordinator(db, "Nora (North coordinator)", north)
        coord_south = _get_or_create_coordinator(db, "Sam (South coordinator)", south)
        db.flush()

        now = clock.now()

        def _qual(worker: CareWorker, code: str, days_from: int, days_until: int):
            exists = db.scalars(
                select(Qualification).where(
                    Qualification.worker_id == worker.id, Qualification.code == code
                )
            ).first()
            if exists is None:
                db.add(
                    Qualification(
                        worker_id=worker.id,
                        code=code,
                        valid_from=now - timedelta(days=days_from),
                        valid_until=now + timedelta(days=days_until),
                    )
                )

        alice = _get_or_create_worker(db, "Alice Aalto", north, "Europe/Helsinki")
        ben = _get_or_create_worker(db, "Ben Berg", north, "Europe/Helsinki")
        carol = _get_or_create_worker(db, "Carol Chen", north, "UTC")
        db.flush()

        # Alice and Ben are fully qualified; Carol's cert expires in 3 days so
        # later visits demonstrate qualification-expiry blocking.
        _qual(alice, "personal_care", 30, 365)
        _qual(alice, "moving_handling", 30, 365)
        _qual(ben, "personal_care", 30, 365)
        _qual(carol, "personal_care", 30, 3)
        db.commit()

        existing = db.scalars(
            select(CarePlan).where(CarePlan.care_recipient_id == "recipient-1001")
        ).all()
        if existing:
            print(f"seed plans already present ({len(existing)}); regenerating tasks")
            for plan in existing:
                stats = plans_service.generate_tasks(db, plan, actor="seed")
                print(f"  plan {plan.id} v{plan.version}: {stats.created} created")
            return

        bathing = plans_service.create_plan(
            db,
            PlanIn(
                care_recipient_id="recipient-1001",
                unit_id=north.id,
                service_timezone="Europe/Helsinki",
                recurrence=Recurrence.daily,
                window_start="08:00",
                window_end="10:00",
                duration_minutes=60,
                required_qualifications=["personal_care"],
            ),
            actor="seed",
        )
        dressing = plans_service.create_plan(
            db,
            PlanIn(
                care_recipient_id="recipient-1001",
                unit_id=north.id,
                service_timezone="Europe/Helsinki",
                recurrence=Recurrence.daily,
                window_start="10:30",
                window_end="12:00",
                duration_minutes=45,
                required_qualifications=["moving_handling"],
                prerequisite_plan_ids=[bathing.id],
            ),
            actor="seed",
        )
        for plan in (bathing, dressing):
            stats = plans_service.generate_tasks(db, plan, actor="seed")
            print(
                f"plan {plan.id} ({plan.recurrence.value}) -> "
                f"{stats.created} tasks over {stats.horizon_days} days"
            )

        print("\nSeed summary")
        print(f"  units: north={north.id} south={south.id}")
        print(f"  coordinators: nora={coord_north.id} (north), sam={coord_south.id} (south)")
        print(f"  workers: alice={alice.id} ben={ben.id} carol={carol.id}")
        print(f"  plans: bathing={bathing.id}, dressing={dressing.id}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
