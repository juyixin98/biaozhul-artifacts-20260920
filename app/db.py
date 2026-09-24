"""SQLite 持久化层。

并发模型
========
SQLite 以 WAL 模式打开，写事务使用 ``BEGIN IMMEDIATE`` 立即获取写锁，并设置
``busy_timeout``。规划本身（CPU 密集）在事务外、基于一致性快照完成；提交时再开
写事务并重新比对地图/预约版本，配合 ``vertex_res`` 的主键约束兜底，保证并发的
两个预约请求不会把同一格同一刻（或同一对向边）同时写进去。

预约行（``reservations``）在撤销后保留作为审计记录，但 ``vertex_res``/``edge_res``
占用行会删除——时空占用表只反映当前生效预约，因此其主键约束天然只约束活跃预约。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .crypto import generate_secret
from .planner import Cell, GridMap

SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maps (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    version    INTEGER NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS obstacles (
    map_id INTEGER NOT NULL REFERENCES maps(id),
    x      INTEGER NOT NULL,
    y      INTEGER NOT NULL,
    PRIMARY KEY (map_id, x, y)
);

CREATE TABLE IF NOT EXISTS robots (
    robot_id   TEXT PRIMARY KEY,
    start_x    INTEGER NOT NULL,
    start_y    INTEGER NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    resv_id       TEXT PRIMARY KEY,
    robot_id      TEXT NOT NULL UNIQUE,
    start_time    INTEGER NOT NULL,
    arrival_time  INTEGER NOT NULL,
    goal_x        INTEGER NOT NULL,
    goal_y        INTEGER NOT NULL,
    map_version   INTEGER NOT NULL,
    created_at    REAL NOT NULL,
    canceled_at   REAL,
    cancel_reason TEXT
);

-- 时空占用：仅保留活跃预约的行；(时间, 格子) 主键在写事务里兜底防同刻同格。
CREATE TABLE IF NOT EXISTS vertex_res (
    resv_id TEXT NOT NULL,
    time    INTEGER NOT NULL,
    x       INTEGER NOT NULL,
    y       INTEGER NOT NULL,
    PRIMARY KEY (time, x, y)
);

-- 有向边（等待不入表）。对向交换冲突在规划/复核时按无向边检查。
CREATE TABLE IF NOT EXISTS edge_res (
    resv_id TEXT NOT NULL,
    time    INTEGER NOT NULL,
    src_x   INTEGER NOT NULL,
    src_y   INTEGER NOT NULL,
    dst_x   INTEGER NOT NULL,
    dst_y   INTEGER NOT NULL,
    PRIMARY KEY (time, src_x, src_y, dst_x, dst_y)
);
"""

# 默认演示地图：8x5，中间一行 y=2 是东西向单行窄道，(2,1) 与 (5,3) 是两个会让湾，
# 机器人可据此在窄道里错车。
DEFAULT_WIDTH = 8
DEFAULT_HEIGHT = 5
DEFAULT_OBSTACLES: List[Cell] = [
    (x, 1) for x in range(1, 7) if x != 2
] + [
    (x, 3) for x in range(1, 7) if x != 5
]
DEFAULT_ROBOTS: List[Tuple[str, Cell]] = [
    ("R1", (0, 0)),
    ("R2", (1, 0)),
    ("R3", (2, 0)),
    ("R4", (3, 0)),
    ("R5", (4, 4)),
    ("R6", (5, 4)),
    ("R7", (6, 4)),
    ("R8", (7, 4)),
]


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def immediate_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """开启 IMMEDIATE 写事务（拿写锁），提交或回滚。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_db(conn: sqlite3.Connection) -> None:
    """建表；全新数据库则写入默认地图、R1..R8、密钥与预约版本 0。"""
    conn.executescript(SCHEMA)
    conn.commit()
    row = conn.execute("SELECT value FROM metadata WHERE key='server_secret'").fetchone()
    if row is None:
        reset_to_default(conn)


def reset_to_default(conn: sqlite3.Connection) -> None:
    """清空并恢复默认场景（在调用方持有的写事务中执行）。"""
    for tbl in (
        "edge_res",
        "vertex_res",
        "reservations",
        "robots",
        "obstacles",
        "maps",
    ):
        conn.execute(f"DELETE FROM {tbl}")
    now = time.time()
    conn.execute(
        "INSERT INTO maps(id,width,height,version,updated_at) VALUES(1,?,?,?,?)",
        (DEFAULT_WIDTH, DEFAULT_HEIGHT, 1, now),
    )
    conn.executemany(
        "INSERT INTO obstacles(map_id,x,y) VALUES(1,?,?)",
        DEFAULT_OBSTACLES,
    )
    conn.executemany(
        "INSERT INTO robots(robot_id,start_x,start_y,created_at) VALUES(?,?,?,?)",
        [(rid, x, y, now) for rid, (x, y) in DEFAULT_ROBOTS],
    )
    _set_meta(conn, "reservation_version", "0")
    if _get_meta(conn, "server_secret") is None:
        _set_meta(conn, "server_secret", generate_secret())


# ---------------------------------------------------------------- 元数据/版本


def _get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO metadata(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def get_server_secret(conn: sqlite3.Connection) -> str:
    return _get_meta(conn, "server_secret")  # type: ignore[return-value]


def get_reservation_version(conn: sqlite3.Connection) -> int:
    return int(_get_meta(conn, "reservation_version") or "0")


def bump_reservation_version(conn: sqlite3.Connection, by: int = 1) -> int:
    newv = get_reservation_version(conn) + by
    _set_meta(conn, "reservation_version", str(newv))
    return newv


# ---------------------------------------------------------------- 读取快照


def load_map(conn: sqlite3.Connection) -> Tuple[GridMap, int]:
    m = conn.execute("SELECT width,height,version FROM maps WHERE id=1").fetchone()
    if m is None:
        raise RuntimeError("地图不存在")
    obs = [(r["x"], r["y"]) for r in conn.execute("SELECT x,y FROM obstacles WHERE map_id=1")]
    return GridMap(m["width"], m["height"], obs), m["version"]


def load_robots(conn: sqlite3.Connection) -> Dict[str, Cell]:
    return {
        r["robot_id"]: (r["start_x"], r["start_y"])
        for r in conn.execute("SELECT robot_id,start_x,start_y FROM robots")
    }


def load_active_reservations(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """读取所有活跃预约（含逐刻占用与边），供构建约束与状态展示。"""
    resvs = conn.execute(
        "SELECT resv_id,robot_id,start_time,arrival_time,goal_x,goal_y,map_version,created_at "
        "FROM reservations WHERE canceled_at IS NULL ORDER BY created_at, robot_id"
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in resvs:
        verts = conn.execute(
            "SELECT time,x,y FROM vertex_res WHERE resv_id=? ORDER BY time",
            (r["resv_id"],),
        ).fetchall()
        edges = conn.execute(
            "SELECT time,src_x,src_y,dst_x,dst_y FROM edge_res WHERE resv_id=? ORDER BY time",
            (r["resv_id"],),
        ).fetchall()
        out.append(
            {
                "resv_id": r["resv_id"],
                "robot_id": r["robot_id"],
                "start_time": r["start_time"],
                "arrival_time": r["arrival_time"],
                "goal": (r["goal_x"], r["goal_y"]),
                "map_version": r["map_version"],
                "created_at": r["created_at"],
                "path": [(v["x"], v["y"]) for v in verts],
                "edges": [
                    (e["time"], (e["src_x"], e["src_y"]), (e["dst_x"], e["dst_y"]))
                    for e in edges
                ],
            }
        )
    return out


# ---------------------------------------------------------------- 写入


def replace_map(
    conn: sqlite3.Connection, grid: GridMap
) -> Tuple[int, bool]:
    """用新障碍替换地图；内容有变化才把地图版本 +1。返回 (版本, 是否变化)。"""
    old_grid, old_version = load_map(conn)
    changed = (
        old_grid.width != grid.width
        or old_grid.height != grid.height
        or old_grid.obstacles != grid.obstacles
    )
    conn.execute("DELETE FROM obstacles WHERE map_id=1")
    conn.execute(
        "UPDATE maps SET width=?, height=?, updated_at=? WHERE id=1",
        (grid.width, grid.height, time.time()),
    )
    conn.executemany(
        "INSERT INTO obstacles(map_id,x,y) VALUES(1,?,?)", list(grid.obstacles)
    )
    new_version = old_version + 1 if changed else old_version
    conn.execute("UPDATE maps SET version=? WHERE id=1", (new_version,))
    return new_version, changed


def insert_reservation(
    conn: sqlite3.Connection,
    *,
    resv_id: str,
    robot_id: str,
    path: List[Cell],
    start_time: int,
    arrival_time: int,
    map_version: int,
) -> None:
    goal_x, goal_y = path[-1]
    conn.execute(
        "INSERT INTO reservations(resv_id,robot_id,start_time,arrival_time,"
        "goal_x,goal_y,map_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            resv_id,
            robot_id,
            start_time,
            arrival_time,
            goal_x,
            goal_y,
            map_version,
            time.time(),
        ),
    )
    conn.executemany(
        "INSERT INTO vertex_res(resv_id,time,x,y) VALUES(?,?,?,?)",
        [(resv_id, start_time + k, x, y) for k, (x, y) in enumerate(path)],
    )
    conn.executemany(
        "INSERT INTO edge_res(resv_id,time,src_x,src_y,dst_x,dst_y) "
        "VALUES(?,?,?,?,?,?)",
        [
            (resv_id, start_time + k, sx, sy, dx, dy)
            for k, ((sx, sy), (dx, dy)) in enumerate(zip(path, path[1:]))
            if (sx, sy) != (dx, dy)
        ],
    )


def cancel_reservation_rows(conn: sqlite3.Connection, resv_id: str, reason: str) -> bool:
    """删除时空占用并把预约标记为撤销；返回它此前是否活跃。"""
    row = conn.execute(
        "SELECT resv_id FROM reservations WHERE resv_id=? AND canceled_at IS NULL",
        (resv_id,),
    ).fetchone()
    if row is None:
        return False
    conn.execute("DELETE FROM vertex_res WHERE resv_id=?", (resv_id,))
    conn.execute("DELETE FROM edge_res WHERE resv_id=?", (resv_id,))
    conn.execute(
        "UPDATE reservations SET canceled_at=?, cancel_reason=? WHERE resv_id=?",
        (time.time(), reason, resv_id),
    )
    return True


def set_robot_start(conn: sqlite3.Connection, robot_id: str, cell: Cell) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO robots(robot_id,start_x,start_y,created_at) VALUES(?,?,?,?) "
        "ON CONFLICT(robot_id) DO UPDATE SET start_x=excluded.start_x, start_y=excluded.start_y",
        (robot_id, cell[0], cell[1], now),
    )


def map_as_json(grid: GridMap, version: int) -> str:
    return json.dumps({"version": version, **grid.to_json()})
