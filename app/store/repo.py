"""SQLite 持久层：规范、编译模型、诊断、运行日志的索引与存取。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.errors import ComputationFailed, InputError, StateConflict
from app.runlog import LogEntry

SCHEMA = """
CREATE TABLE IF NOT EXISTS specs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(name, version)
);
CREATE TABLE IF NOT EXISTS lexers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_id INTEGER NOT NULL REFERENCES specs(id),
    status TEXT NOT NULL,
    model_json TEXT,
    stats_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lexer_id INTEGER NOT NULL REFERENCES lexers(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts TEXT NOT NULL,
    module TEXT NOT NULL,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    state_json TEXT NOT NULL,
    rationale TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_logs_run ON run_logs(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_lexers_spec ON lexers(spec_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repo:
    """索引与模型层的仓储接口；所有 SQL 参数化。"""

    def __init__(self, db_path: str):
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- 运行日志 ----
    def log_sink(self):
        def sink(entry: LogEntry) -> None:
            row = entry.to_row()
            self._conn.execute(
                "INSERT INTO run_logs (run_id, seq, ts, module, level, event, state_json, rationale)"
                " VALUES (:run_id, :seq, :ts, :module, :level, :event, :state_json, :rationale)",
                row,
            )
            self._conn.commit()

        return sink

    def logs_for_run(self, run_id: str) -> list[dict[str, Any]]:
        cur = self._conn.execute(
            "SELECT run_id, seq, ts, module, level, event, state_json, rationale"
            " FROM run_logs WHERE run_id = ? ORDER BY seq",
            (run_id,),
        )
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            d["state"] = json.loads(d.pop("state_json"))
            rows.append(d)
        return rows

    # ---- 规范 ----
    def create_spec(self, name: str, version: str, body: dict[str, Any]) -> int:
        try:
            cur = self._conn.execute(
                "INSERT INTO specs (name, version, body, created_at) VALUES (?, ?, ?, ?)",
                (name, version, json.dumps(body, ensure_ascii=False), _now()),
            )
            self._conn.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError as e:
            raise StateConflict(
                code="SPEC_EXISTS",
                message=f"规范 {name!r} 版本 {version!r} 已存在",
                details={"name": name, "version": version},
            ) from e

    def get_spec(self, spec_id: int) -> dict[str, Any]:
        cur = self._conn.execute("SELECT * FROM specs WHERE id = ?", (spec_id,))
        row = cur.fetchone()
        if row is None:
            raise InputError(
                code="SPEC_NOT_FOUND",
                message=f"规范 id={spec_id} 不存在",
                details={"spec_id": spec_id},
            )
        d = dict(row)
        d["body"] = json.loads(d["body"])
        return d

    # ---- 词法器模型 ----
    def create_lexer(self, spec_id: int, status: str, model: dict | None, stats: dict | None) -> int:
        cur = self._conn.execute(
            "INSERT INTO lexers (spec_id, status, model_json, stats_json, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                spec_id,
                status,
                json.dumps(model, ensure_ascii=False) if model is not None else None,
                json.dumps(stats, ensure_ascii=False) if stats is not None else None,
                _now(),
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_lexer(self, lexer_id: int) -> dict[str, Any]:
        cur = self._conn.execute("SELECT * FROM lexers WHERE id = ?", (lexer_id,))
        row = cur.fetchone()
        if row is None:
            raise InputError(
                code="LEXER_NOT_FOUND",
                message=f"词法器 id={lexer_id} 不存在",
                details={"lexer_id": lexer_id},
            )
        d = dict(row)
        d["model"] = json.loads(d.pop("model_json")) if d["model_json"] else None
        d["stats"] = json.loads(d.pop("stats_json")) if d["stats_json"] else None
        return d

    def set_lexer_status(self, lexer_id: int, status: str) -> None:
        self._conn.execute("UPDATE lexers SET status = ? WHERE id = ?", (status, lexer_id))
        self._conn.commit()

    def set_lexer_ready(self, lexer_id: int, model: dict, stats: dict) -> None:
        self._conn.execute(
            "UPDATE lexers SET status = ?, model_json = ?, stats_json = ? WHERE id = ?",
            (
                "ready",
                json.dumps(model, ensure_ascii=False),
                json.dumps(stats, ensure_ascii=False),
                lexer_id,
            ),
        )
        self._conn.commit()

    # ---- 诊断 ----
    def add_diagnostics(self, lexer_id: int, kind: str, entries: list[dict[str, Any]]) -> None:
        self._conn.executemany(
            "INSERT INTO diagnostics (lexer_id, kind, payload) VALUES (?, ?, ?)",
            [(lexer_id, kind, json.dumps(e, ensure_ascii=False)) for e in entries],
        )
        self._conn.commit()

    def diagnostics_for(self, lexer_id: int) -> dict[str, list[dict[str, Any]]]:
        cur = self._conn.execute(
            "SELECT kind, payload FROM diagnostics WHERE lexer_id = ? ORDER BY id", (lexer_id,)
        )
        out: dict[str, list[dict[str, Any]]] = {}
        for kind, payload in cur.fetchall():
            out.setdefault(kind, []).append(json.loads(payload))
        return out

    # ---- 防护 ----
    def execute_raw(self, sql: str) -> None:
        raise ComputationFailed(
            code="RAW_SQL_FORBIDDEN",
            message="仓储层不允许裸 SQL；请使用参数化方法",
            details={"sql": sql[:80]},
        )
