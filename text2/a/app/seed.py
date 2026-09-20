"""样例数据：python -m app.seed

创建一个单元、两名护理员（含资格）、一名协调员、两个护理计划
（其中一个带前置任务），并生成未来 14 天任务。可重复运行（幂等）。
"""

from datetime import date, datetime, time, timedelta, timezone

from app.clock import Clock
from app.config import settings
from app.database import SessionLocal
from app.models import (Caregiver, CaregiverQualification, CarePlan,
                        Coordinator, CoordinatorUnit)
from app.services import scheduling


def main() -> None:
    db = SessionLocal()
    now = Clock().now()
    try:
        if db.query(CarePlan).count() > 0:
            print("seed: data already present, skipping")
            return

        unit = "unit-a"
        cg1 = Caregiver(name="Alice", unit_id=unit)
        cg2 = Caregiver(name="Bob", unit_id=unit)
        db.add_all([cg1, cg2])
        db.flush()
        far_future = now + timedelta(days=365)
        for cg in (cg1, cg2):
            db.add(CaregiverQualification(
                caregiver_id=cg.id, code="RN",
                valid_from=now - timedelta(days=30), valid_until=far_future))
        db.add(CaregiverQualification(
            caregiver_id=cg1.id, code="WOUND_CARE",
            valid_from=now - timedelta(days=30), valid_until=far_future))

        coord = Coordinator(name="Carol")
        db.add(coord)
        db.flush()
        db.add(CoordinatorUnit(coordinator_id=coord.id, unit_id=unit))

        today = now.date()
        plan_a = CarePlan(
            name="Morning check", unit_id=unit, patient_name="Patient Zero",
            timezone="UTC", frequency="daily", interval=1,
            window_start=time(8, 0), window_end=time(10, 0),
            duration_minutes=60, required_qualification="RN",
            start_date=today,
        )
        db.add(plan_a)
        db.flush()
        plan_b = CarePlan(
            name="Wound dressing", unit_id=unit, patient_name="Patient Zero",
            timezone="UTC", frequency="weekly", interval=1, byweekday=["MO", "TH"],
            window_start=time(10, 0), window_end=time(12, 0),
            duration_minutes=90, required_qualification="WOUND_CARE",
            prerequisite_plan_id=plan_a.id, start_date=today,
        )
        db.add(plan_b)
        db.flush()

        created = []
        for plan in (plan_a, plan_b):
            created += scheduling.generate_tasks(
                db, plan, now, settings.generation_horizon_days)
        db.commit()
        print(f"seed: unit={unit} caregivers={cg1.id},{cg2.id} "
              f"coordinator={coord.id} plans={plan_a.id},{plan_b.id} "
              f"tasks_created={len(created)}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
