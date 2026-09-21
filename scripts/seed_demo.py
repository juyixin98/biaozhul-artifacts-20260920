"""Seed demo data: org, departments, employees, users, devices and a set of
events that trigger every detection rule. Idempotent: skips if the demo org
already exists.

Run:  python -m scripts.seed_demo
Demo logins (change in production):
  admin   / Admin123!
  manager / Manager123!   (Mandy, sees direct reports alice/bob/carol)
  analyst / Analyst123!   (authorized for Engineering only)
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.database import SessionLocal
from app.models import (
    AnalystDepartment,
    Department,
    Device,
    Employee,
    Organization,
    User,
)
from app.schemas import EventIn
from app.security import hash_password
from app.services.ingestion import ingest_batch

TZ = ZoneInfo("Asia/Shanghai")


def seed() -> None:
    db = SessionLocal()
    try:
        if db.query(Organization).filter(Organization.name == "Demo Corp").first():
            print("Demo data already present, skipping.")
            return

        org = Organization(name="Demo Corp", timezone="Asia/Shanghai")
        db.add(org)
        db.flush()
        eng = Department(org_id=org.id, name="Engineering")
        fin = Department(org_id=org.id, name="Finance")
        db.add_all([eng, fin])
        db.flush()

        mandy = Employee(org_id=org.id, department_id=eng.id, name="Mandy Manager")
        db.add(mandy)
        db.flush()
        alice = Employee(org_id=org.id, department_id=eng.id, name="Alice", manager_id=mandy.id)
        bob = Employee(org_id=org.id, department_id=eng.id, name="Bob", manager_id=mandy.id)
        carol = Employee(org_id=org.id, department_id=fin.id, name="Carol", manager_id=mandy.id)
        db.add_all([alice, bob, carol])
        db.flush()

        admin = User(username="admin", password_hash=hash_password("Admin123!"), role="admin")
        manager = User(
            username="manager",
            password_hash=hash_password("Manager123!"),
            role="manager",
            employee_id=mandy.id,
        )
        analyst = User(username="analyst", password_hash=hash_password("Analyst123!"), role="analyst")
        db.add_all([admin, manager, analyst])
        db.flush()
        db.add(AnalystDepartment(user_id=analyst.id, department_id=eng.id))

        dev_alice = Device(employee_id=alice.id, hostname="alice-laptop")
        dev_bob = Device(employee_id=bob.id, hostname="bob-laptop")
        dev_carol = Device(employee_id=carol.id, hostname="carol-laptop")
        db.add_all([dev_alice, dev_bob, dev_carol])
        db.flush()

        base = datetime(2026, 9, 20, 12, 0, tzinfo=TZ)
        events: list[EventIn] = []

        # Off-hours access for Alice (03:30 local).
        events.append(EventIn(device_id=dev_alice.id, event_id="demo-offhours-1",
                              event_type="access",
                              occurred_at=base.replace(hour=3, minute=30),
                              payload={"resource": "code-repo"}))
        # Download burst for Bob (55 downloads in one minute).
        for i in range(55):
            events.append(EventIn(device_id=dev_bob.id, event_id=f"demo-dl-{i}",
                                  event_type="file_download",
                                  occurred_at=base.replace(hour=14, minute=5),
                                  payload={"file": f"export-{i}.csv"}))
        # First USB connection for Alice's device.
        events.append(EventIn(device_id=dev_alice.id, event_id="demo-usb-1",
                              event_type="usb_connect",
                              occurred_at=base.replace(hour=11, minute=0),
                              payload={"usb_serial": "USB-7788"}))
        # 15 days of varied file-access history for Carol, then a spike today.
        for day in range(15, 0, -1):
            for i in range(2 + day % 4):  # 2..5 accesses/day -> non-zero variance
                events.append(EventIn(device_id=dev_carol.id,
                                      event_id=f"demo-fa-{day}-{i}",
                                      event_type="file_access",
                                      occurred_at=base - timedelta(days=day, hours=1),
                                      payload={"file": f"ledger-{day}-{i}.xlsx"}))
        for i in range(40):
            events.append(EventIn(device_id=dev_carol.id, event_id=f"demo-spike-{i}",
                                  event_type="file_access",
                                  occurred_at=base.replace(hour=10, minute=i % 50),
                                  payload={"file": f"bulk-{i}.xlsx"}))

        result = ingest_batch(db, events)
        db.commit()
        print(f"Seeded demo data: {result}")
        print("Logins: admin/Admin123!  manager/Manager123!  analyst/Analyst123!")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
