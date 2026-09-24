"""初始化数据库并写入示例数据（机器人 / 充电点 / 任务）。

用法：
    python -m scripts.seed            # 覆盖式重建示例数据
"""
from __future__ import annotations

import os

from app import database
from app.database import utcnow

ROBOTS = [
    # id, x, y, battery_wh
    ("R1", 0.0, 0.0, 90.0),     # 电量适中
    ("R2", 2.0, 0.0, 400.0),    # 电量充足
    ("R3", 49.0, 1.0, 15.0),    # 在任务旁、局部最便宜但无法保留余量返航
]
CHARGERS = [
    ("C1", 0.0, 0.0),
    ("C2", 30.0, 0.0),
]
TASKS = [
    # id, x, y, payload_kg, wait_s
    ("T1", 50.0, 0.0, 20.0, 60.0),
    ("T2", 30.0, 5.0, 10.0, 30.0),
]


def main() -> None:
    if os.path.exists(database.config.DB_PATH):
        os.remove(database.config.DB_PATH)
    for suffix in ("-wal", "-shm"):
        p = database.config.DB_PATH + suffix
        if os.path.exists(p):
            os.remove(p)
    database.init_db()
    conn = database.connect()
    try:
        with database.immediate_tx(conn):
            for rid, x, y, b in ROBOTS:
                conn.execute(
                    "INSERT INTO robots(id,x,y,battery_wh,status,updated_at)"
                    " VALUES (?,?,?,?,'idle',?)",
                    (rid, x, y, b, utcnow()),
                )
            for cid, x, y in CHARGERS:
                conn.execute(
                    "INSERT INTO chargers(id,x,y,status) VALUES (?,?,?,"
                    "'available')",
                    (cid, x, y),
                )
            for tid, x, y, kg, w in TASKS:
                conn.execute(
                    "INSERT INTO tasks(id,x,y,payload_kg,wait_s,status,"
                    "created_at) VALUES (?,?,?,?,?,'pending',?)",
                    (tid, x, y, kg, w, utcnow()),
                )
        print(f"seeded db at {database.config.DB_PATH}")
        print(f"  robots:  {[r[0] for r in ROBOTS]}")
        print(f"  chargers:{[c[0] for c in CHARGERS]}")
        print(f"  tasks:   {[t[0] for t in TASKS]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
