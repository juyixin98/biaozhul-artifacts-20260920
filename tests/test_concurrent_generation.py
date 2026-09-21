"""Concurrent generation must never double-create occurrences."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Thread

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import make_engine
from app.models import CarePlan, Task
from app.services.generation import generate_tasks

MON = datetime(2026, 9, 21, 0, 0, tzinfo=timezone.utc)


def test_concurrent_generation_is_idempotent(db: Session):
    from tests.conftest import make_unit

    unit = make_unit(db)
    plan = CarePlan(
        external_id="CONC-GEN", revision=1, active=True, client_name="C",
        unit_id=unit.id, timezone="UTC", created_at=MON,
    )
    db.add(plan)
    db.commit()

    # Templates exist (created via simple insert to keep the test focused).
    from app.models import PlanTaskTemplate
    db.add(PlanTaskTemplate(
        plan_id=plan.id, code="V", name="Visit",
        window_start_minute=10 * 60, window_end_minute=12 * 60,
        duration_minutes=30, weekday_mask=[], qualification_codes=[],
    ))
    db.commit()

    url = db.bind.url.render_as_string(hide_password=False)
    engine = make_engine(url)
    errors: list[Exception] = []

    def generate() -> None:
        own = Session(engine)
        try:
            p = own.get(CarePlan, plan.id)
            generate_tasks(own, p, now=MON)
            own.commit()
        except Exception as exc:  # one side may hit the unique constraint
            own.rollback()
            errors.append(exc)
        finally:
            own.close()

    threads = [Thread(target=generate) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check = Session(engine)
    count = check.scalar(select(func.count()).select_from(Task).where(Task.plan_id == plan.id))
    check.close()
    engine.dispose()

    # Exactly 14 rows however many threads won the insert race; any losers
    # must have failed on the natural-key unique constraint, not corrupted data.
    assert count == 14
    if errors:
        assert all("uq_task_plan_template_date" in str(e) or "UniqueViolation" in type(e).__name__
                   for e in errors)
