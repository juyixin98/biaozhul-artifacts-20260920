"""SQLite 持久层。

表结构：
- maps               地图（尺寸、障碍、地图版本、内容哈希、预订版本）
- reservations       每条激活预订（含完整路径、凭证哈希）
- reservation_vertex (reservation, t, cell) 顶点占用
- reservation_edge   (reservation, t, a, b) 区间 [t,t+1] 的有向移动
                     对 (t,a,b) 做 UNIQUE：对向交换双方规范成同一条无向边，
                     数据库层即可拦住对向交换，作为应用层之外的纵深防御。
- reservation_endpoint 终点停留（reservation, cell, arrival）
- audit_log          所有创建/撤销/地图变更操作的审计记录

写入全部在一个进程内的 threading.Lock 下串行化（本服务为单实例小规模服务）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

Cell = tuple[int, int]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str = "stp.db"):
        self.path = path
        # check_same_thread=False：我们自行用锁保证串行
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.write_lock = threading.RLock()
        self._apply_pragmas()
        self.init_schema()

    def _apply_pragmas(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")

    def init_schema(self) -> None:
        with self.write_lock:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS maps (
                    map_id      TEXT PRIMARY KEY,
                    width       INTEGER NOT NULL,
                    height      INTEGER NOT NULL,
                    obstacles   TEXT NOT NULL DEFAULT '[]',
                    map_version INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    res_version INTEGER NOT NULL DEFAULT 0,
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    map_id         TEXT NOT NULL REFERENCES maps(map_id),
                    robot_id       INTEGER NOT NULL,
                    start_t        INTEGER NOT NULL DEFAULT 0,
                    horizon        INTEGER NOT NULL,
                    path           TEXT NOT NULL,
                    actions        TEXT NOT NULL,
                    token_hash     TEXT NOT NULL,
                    status         TEXT NOT NULL DEFAULT 'active',
                    created_at     TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_robot
                    ON reservations(map_id, robot_id)
                    WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS reservation_vertex (
                    reservation_id TEXT NOT NULL
                        REFERENCES reservations(reservation_id) ON DELETE CASCADE,
                    map_id TEXT NOT NULL,
                    t INTEGER NOT NULL,
                    x INTEGER NOT NULL,
                    y INTEGER NOT NULL,
                    robot_id INTEGER NOT NULL,
                    PRIMARY KEY (reservation_id, t)
                );
                CREATE INDEX IF NOT EXISTS idx_vertex_cell_t
                    ON reservation_vertex(map_id, x, y, t);

                CREATE TABLE IF NOT EXISTS reservation_edge (
                    reservation_id TEXT NOT NULL
                        REFERENCES reservations(reservation_id) ON DELETE CASCADE,
                    map_id TEXT NOT NULL,
                    t INTEGER NOT NULL,
                    ax INTEGER NOT NULL,
                    ay INTEGER NOT NULL,
                    bx INTEGER NOT NULL,
                    by INTEGER NOT NULL,
                    robot_id INTEGER NOT NULL,
                    UNIQUE(map_id, t, ax, ay, bx, by)
                );

                CREATE TABLE IF NOT EXISTS reservation_endpoint (
                    reservation_id TEXT PRIMARY KEY
                        REFERENCES reservations(reservation_id) ON DELETE CASCADE,
                    map_id TEXT NOT NULL,
                    robot_id INTEGER NOT NULL,
                    x INTEGER NOT NULL,
                    y INTEGER NOT NULL,
                    arrival INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_endpoint_cell
                    ON reservation_endpoint(map_id, x, y);

                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    map_id TEXT,
                    reservation_id TEXT,
                    action TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------ maps
    def create_map(
        self,
        map_id: str,
        width: int,
        height: int,
        obstacles: list[Cell],
        content_hash: str,
    ) -> None:
        with self.write_lock:
            self.conn.execute(
                "INSERT INTO maps(map_id, width, height, obstacles, map_version,"
                " content_hash, res_version, created_at) VALUES (?,?,?,?,1,?,0,?)",
                (map_id, width, height, json.dumps(sorted(obstacles)),
                 content_hash, _now()),
            )

    def get_map(self, map_id: str) -> sqlite3.Row | None:
        with self.write_lock:
            return self.conn.execute(
                "SELECT * FROM maps WHERE map_id=?", (map_id,)
            ).fetchone()

    def update_map_obstacles(
        self,
        map_id: str,
        obstacles: list[Cell],
        content_hash: str,
    ) -> None:
        """障碍变更：地图版本 +1（乐观锁：地图变化使在途规划失效）。"""
        with self.write_lock:
            self.conn.execute(
                "UPDATE maps SET obstacles=?, content_hash=?, map_version="
                "map_version+1 WHERE map_id=?",
                (json.dumps(sorted(obstacles)), content_hash, map_id),
            )

    def bump_res_version(self, map_id: str) -> int:
        with self.write_lock:
            self.conn.execute(
                "UPDATE maps SET res_version=res_version+1 WHERE map_id=?",
                (map_id,),
            )
            row = self.conn.execute(
                "SELECT res_version FROM maps WHERE map_id=?", (map_id,)
            ).fetchone()
            return int(row["res_version"])

    # ----------------------------------------------------------- reservations
    def active_reservations(self, map_id: str) -> list[sqlite3.Row]:
        with self.write_lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM reservations WHERE map_id=? AND status='active'"
                    " ORDER BY robot_id ASC",
                    (map_id,),
                ).fetchall()
            )

    def get_reservation(self, reservation_id: str) -> sqlite3.Row | None:
        with self.write_lock:
            return self.conn.execute(
                "SELECT * FROM reservations WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone()

    def delete_active_reservation_rows(self, map_id: str, robot_id: int) -> None:
        """删除某机器人在该图上的旧激活预订（重规划替换），须在事务内调用。"""
        row = self.conn.execute(
            "SELECT reservation_id FROM reservations WHERE map_id=? AND robot_id=? "
            "AND status='active'",
            (map_id, robot_id),
        ).fetchone()
        if row is not None:
            rid = row["reservation_id"]
            for tbl in (
                "reservation_vertex",
                "reservation_edge",
                "reservation_endpoint",
                "reservations",
            ):
                self.conn.execute(
                    f"DELETE FROM {tbl} WHERE reservation_id=?", (rid,)
                )

    def insert_reservation(
        self,
        *,
        reservation_id: str,
        map_id: str,
        robot_id: int,
        horizon: int,
        path: list[Cell],
        actions: list[dict[str, Any]],
        token_hash: str,
    ) -> None:
        """写入预订及其全部顶点/边/终点索引。调用方必须已开启 BEGIN 事务。

        边冲突的 UNIQUE 约束在此层兜底：若两个对向移动规范到同一条无向边，
        INSERT 直接抛 sqlite3.IntegrityError。
        """
        self.conn.execute(
            "INSERT INTO reservations(reservation_id, map_id, robot_id, "
            "start_t, horizon, path, actions, token_hash, status, created_at) "
            "VALUES (?,?,?,0,?,?,?,?,'active',?)",
            (
                reservation_id,
                map_id,
                robot_id,
                horizon,
                json.dumps([list(c) for c in path]),
                json.dumps(actions),
                token_hash,
                _now(),
            ),
        )
        for t, cell in enumerate(path):
            self.conn.execute(
                "INSERT INTO reservation_vertex(reservation_id, t, x, y, "
                "robot_id, map_id) VALUES (?,?,?,?,?,?)",
                (reservation_id, t, cell[0], cell[1], robot_id, map_id),
            )
        for t, (u, v) in enumerate(zip(path, path[1:])):
            if u == v:
                continue
            a, b = (u, v) if u <= v else (v, u)
            self.conn.execute(
                "INSERT INTO reservation_edge(reservation_id, map_id, t, ax, ay, "
                "bx, by, robot_id) VALUES (?,?,?,?,?,?,?,?)",
                (reservation_id, map_id, t, a[0], a[1], b[0], b[1], robot_id),
            )
        goal = path[-1]
        self.conn.execute(
            "INSERT INTO reservation_endpoint(reservation_id, map_id, robot_id, "
            "x, y, arrival) VALUES (?,?,?,?,?,?)",
            (reservation_id, map_id, robot_id, goal[0], goal[1], len(path) - 1),
        )

    def cancel_reservation(self, reservation_id: str) -> None:
        """逻辑撤销 + 物理删除占用索引（审计行保留）。"""
        with self.write_lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.execute(
                    "UPDATE reservations SET status='cancelled' "
                    "WHERE reservation_id=?",
                    (reservation_id,),
                )
                for tbl in (
                    "reservation_vertex",
                    "reservation_edge",
                    "reservation_endpoint",
                ):
                    self.conn.execute(
                        f"DELETE FROM {tbl} WHERE reservation_id=?",
                        (reservation_id,),
                    )
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def audit(
        self,
        action: str,
        detail: dict[str, Any],
        *,
        map_id: str | None = None,
        reservation_id: str | None = None,
    ) -> None:
        with self.write_lock:
            self.conn.execute(
                "INSERT INTO audit_log(ts, map_id, reservation_id, action, "
                "detail) VALUES (?,?,?,?,?)",
                (_now(), map_id, reservation_id, action, json.dumps(detail)),
            )

    def close(self) -> None:
        self.conn.close()
