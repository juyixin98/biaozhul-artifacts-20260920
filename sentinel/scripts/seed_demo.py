"""Idempotent demo-data seed.

Run: python -m scripts.seed_demo
All demo passwords are: Passw0rd!

Produces:
  * org tree: HQ / Engineering (America/New_York) / Sales (Asia/Shanghai)
  * admin, analyst (Engineering scope), two managers, four employees
  * sam   -> after-hours alert, 51-download burst, file-access baseline anomaly
  * evan  -> first-USB alert
  * zoe   -> zero-variance baseline status
  * neo   -> insufficient-history baseline status
  * yuki  -> after-hours event (Sales), invisible to the Engineering analyst
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database import SessionLocal
from app.detection.baseline import compute_baseline
from app.detection.engine import ingest_batch
from app.models import Device, EventType, Organization, User, UserRole
from app.schemas import EventIn
from app.security import hash_password

PASSWORD = "Passw0rd!"


def _ev(device_key: str, event_id: str, ev_type: EventType, when: datetime, **payload) -> EventIn:
    return EventIn(
        event_id=event_id,
        device_key=device_key,
        type=ev_type,
        occurred_at=when,
        payload=payload,
    )


def seed() -> None:
    db = SessionLocal()
    try:
        if db.scalars(select(User).where(User.username == "admin")).first():
            print("demo data already present; skipping")
            return

        ny = ZoneInfo("America/New_York")
        sh = ZoneInfo("Asia/Shanghai")

        hq = Organization(id=1, name="HQ", path="/1/", timezone="UTC", parent_id=None)
        eng = Organization(id=2, name="Engineering", path="/1/2/", timezone="America/New_York", parent_id=1)
        sales = Organization(id=3, name="Sales", path="/1/3/", timezone="Asia/Shanghai", parent_id=1)
        db.add_all([hq, eng, sales])
        db.flush()

        def user(uid, username, full, role, org, manager=None):
            u = User(
                id=uid, username=username, password_hash=hash_password(PASSWORD),
                full_name=full, role=role, org_id=org, manager_id=manager, is_active=True,
            )
            db.add(u)
            return u

        admin = user(1, "admin", "Ada Admin", UserRole.admin, 1)
        raul = user(2, "raul", "Raul Manager", UserRole.manager, 2)
        maya = user(3, "maya", "Maya Manager", UserRole.manager, 3)
        analyst = user(4, "lee", "Lee Analyst", UserRole.analyst, 1)
        sam = user(5, "sam", "Sam Engineer", UserRole.employee, 2, manager=2)
        evan = user(6, "evan", "Evan Engineer", UserRole.employee, 2, manager=2)
        neo = user(7, "neo", "Neo Newhire", UserRole.employee, 2, manager=2)
        zoe = user(8, "zoe", "Zoe Sales", UserRole.employee, 3, manager=3)
        yuki = user(9, "yuki", "Yuki Sales", UserRole.employee, 3, manager=3)
        db.flush()

        # Analyst authorized for the Engineering subtree only.
        from app.models import DepartmentMembership

        db.add(DepartmentMembership(user_id=analyst.id, department_id=eng.id))

        devices = {}
        for uid, key, name in [
            (5, "dev-sam", "Sam's laptop"),
            (6, "dev-evan", "Evan's laptop"),
            (7, "dev-neo", "Neo's laptop"),
            (8, "dev-zoe", "Zoe's laptop"),
            (9, "dev-yuki", "Yuki's laptop"),
        ]:
            d = Device(device_key=key, user_id=uid, name=name)
            db.add(d)
            devices[uid] = key
        db.commit()

        now = datetime.now(timezone.utc)
        ny_yesterday = (now.astimezone(ny) - timedelta(days=1)).date()
        sh_yesterday = (now.astimezone(sh) - timedelta(days=1)).date()

        # --- Sam: 14 days low-variance history, then a 200-event spike day ---
        hist = [10, 12, 9, 11, 10, 13, 9, 10, 12, 11, 10, 9, 12, 10]
        batch = []
        for i, count in enumerate(hist, start=1):
            day = ny_yesterday - timedelta(days=15 - i)
            for j in range(count):
                when = datetime(day.year, day.month, day.day, 12, j % 60, tzinfo=ny)
                batch.append(_ev("dev-sam", f"sam-h-{i}-{j}", EventType.file_access, when,
                                 file=f"/share/report_{j}.doc"))
        for j in range(200):
            day = ny_yesterday
            when = datetime(day.year, day.month, day.day, 11, j % 60, tzinfo=ny)
            batch.append(_ev("dev-sam", f"sam-spike-{j}", EventType.file_access, when,
                             file=f"/secret/data_{j}.xlsx"))
        # 51 downloads inside 9 minutes -> burst.
        for j in range(51):
            day = ny_yesterday
            when = datetime(day.year, day.month, day.day, 14, j // 10, (j % 10) * 6, tzinfo=ny)
            batch.append(_ev("dev-sam", f"sam-dl-{j}", EventType.file_download, when,
                             file=f"/downloads/bundle_{j}.zip"))
        # After-hours event at 03:10 local.
        batch.append(_ev(
            "dev-sam", "sam-night-1", EventType.website_visit,
            datetime(ny_yesterday.year, ny_yesterday.month, ny_yesterday.day, 3, 10, tzinfo=ny),
            url="https://example.personal.net/upload",
        ))
        ingest_batch(db, batch)
        db.commit()

        # --- Evan: normal history + first USB at 10:00 local ---
        evan_batch = [
            _ev("dev-evan", f"evan-h-{i}", EventType.file_access,
                datetime(
                    (ny_yesterday - timedelta(days=i)).year,
                    (ny_yesterday - timedelta(days=i)).month,
                    (ny_yesterday - timedelta(days=i)).day,
                    10, 0, tzinfo=ny),
                file="/handbook.pdf")
            for i in range(1, 8)
        ]
        evan_batch.append(_ev(
            "dev-evan", "evan-usb-1", EventType.usb_connect,
            datetime(ny_yesterday.year, ny_yesterday.month, ny_yesterday.day, 10, 0, tzinfo=ny),
            vendor="GenericFlash", serial="USB0001",
        ))
        ingest_batch(db, evan_batch)
        db.commit()

        # --- Zoe: exactly 8 events/day over 14 complete days -> zero variance ---
        zoe_batch = []
        for i in range(1, 15):
            day = sh_yesterday - timedelta(days=i)
            for j in range(8):
                when = datetime(day.year, day.month, day.day, 9, j, tzinfo=sh)
                zoe_batch.append(_ev("dev-zoe", f"zoe-h-{i}-{j}", EventType.file_access, when,
                                     file=f"/crm/lead_{j}.csv"))
        # A late-night website visit -> off-hours alert (22:30 Shanghai).
        zoe_batch.append(_ev(
            "dev-zoe", "zoe-night-1", EventType.website_visit,
            datetime(sh_yesterday.year, sh_yesterday.month, sh_yesterday.day, 22, 30, tzinfo=sh),
            url="https://shopping.example.com",
        ))
        ingest_batch(db, zoe_batch)
        db.commit()

        # --- Neo: only 3 days of history -> insufficient history ---
        neo_batch = [
            _ev("dev-neo", f"neo-h-{i}", EventType.file_access,
                datetime(
                    (ny_yesterday - timedelta(days=i)).year,
                    (ny_yesterday - timedelta(days=i)).month,
                    (ny_yesterday - timedelta(days=i)).day,
                    11, 0, tzinfo=ny))
            for i in range(1, 4)
        ]
        ingest_batch(db, neo_batch)
        db.commit()

        # --- Yuki: ordinary, used for manager/Sales access checks ---
        yuki_batch = [
            _ev("dev-yuki", "yuki-1", EventType.login,
                datetime(sh_yesterday.year, sh_yesterday.month, sh_yesterday.day, 9, 0, tzinfo=sh))
        ]
        ingest_batch(db, yuki_batch)
        db.commit()

        # --- Baselines (versioned) ---
        for uid, label in [(5, "sam"), (8, "zoe"), (7, "neo")]:
            tz = ny if uid in (5, 7) else sh
            target = (now.astimezone(tz) - timedelta(days=1)).date()
            baseline, alert_id = compute_baseline(db, uid, target)
            db.commit()
            print(f"baseline {label}: v{baseline.version} status={baseline.status.value} "
                  f"z={baseline.zscore} alert={alert_id}")

        print("seed complete. logins (password: %s):" % PASSWORD)
        for name in ["admin", "lee", "raul", "maya", "sam", "evan", "neo", "zoe", "yuki"]:
            print(f"  - {name}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
