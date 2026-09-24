"""Idempotent demo seed: robots, chargers, tasks and a demo operator.

Run:  python -m examples.seed_demo --reset
"""
from __future__ import annotations

import argparse

from app import config
from app.database import connect, init_db, transaction
from app import services
from app.schemas import ChargerIn, RobotIn, TaskIn

ROBOTS = [
    # R1: cheap/local for nearby work, but too little battery to ever reach
    # a charger after a long outbound leg (tests locally-cheapest-unreachable)
    RobotIn(id="R1", name="Scout-LowBattery", position=(0.0, 0.0),
            current_soc_kwh=4.0, capacity_kwh=4.0,
            payload_capacity_kg=20),
    RobotIn(id="R2", name="Hauler-A", position=(5.0, 0.0),
            current_soc_kwh=80.0, capacity_kwh=100.0,
            payload_capacity_kg=200),
    RobotIn(id="R3", name="Hauler-B", position=(200.0, 0.0),
            current_soc_kwh=90.0, capacity_kwh=100.0,
            payload_capacity_kg=200),
]

CHARGERS = [
    ChargerIn(id="C1", name="Depot-West", position=(0.0, 0.0)),
    ChargerIn(id="C2", name="Hub-Center", position=(100.0, 0.0)),
]

TASKS = [
    TaskIn(id="T1", title="Dock-to-far-yard delivery",
           pickup=(0.0, 0.0), delivery=(60.0, 0.0),
           payload_kg=40, wait_seconds=120, priority=8),
    TaskIn(id="T2", title="Second simultaneous haul",
           pickup=(10.0, 0.0), delivery=(55.0, 0.0),
           payload_kg=30, wait_seconds=60, priority=5),
]


def seed(reset: bool = False) -> None:
    if reset:
        for suffix in ("", "-wal", "-shm"):
            p = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
            p.unlink(missing_ok=True)
    init_db()
    conn = connect()
    try:
        with transaction(conn):
            op = services.get_operator_by_username(conn,
                                                   config.DEMO_OPERATOR[0])
            if op is None:
                services.create_operator(conn, *config.DEMO_OPERATOR)
        # application-level CRUD keeps ids unique / seed idempotent
        existing_robots = {r["id"] for r in services.list_robots()}
        for r in ROBOTS:
            if r.id not in existing_robots:
                services.create_robot(r)
        existing_chargers = {c["id"] for c in services.list_chargers()}
        for c in CHARGERS:
            if c.id not in existing_chargers:
                services.create_charger(c)
        existing_tasks = {t["id"] for t in services.list_tasks()}
        for t in TASKS:
            if t.id not in existing_tasks:
                services.create_task(t)
    finally:
        conn.close()
    print(f"Seeded {len(ROBOTS)} robots, {len(CHARGERS)} chargers, "
          f"{len(TASKS)} tasks; demo operator "
          f"'{config.DEMO_OPERATOR[0]}' / '{config.DEMO_OPERATOR[1]}'.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true",
                    help="delete the database file before seeding")
    args = ap.parse_args()
    seed(reset=args.reset)
