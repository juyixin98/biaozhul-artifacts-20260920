"""事件与版本报告的持久化。

提供两种实现:
* :class:`MemoryRepo` —— 进程内字典, 供无数据库的单元测试使用
* :class:`PostgresRepo` —— PostgreSQL, 原始事件只追加、历史报告只追加
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    ts         BIGINT NOT NULL,
    seq        BIGINT NOT NULL,
    action     TEXT NOT NULL,
    payload    JSONB NOT NULL,
    signer     TEXT NOT NULL,
    signature  TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reports (
    version BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    hash TEXT NOT NULL,
    body JSONB NOT NULL,
    signature TEXT NOT NULL,
    change_reason TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS reports_version_idx ON reports (version DESC);
"""

ENGINE_FIELDS = ("event_id", "ts", "seq", "action", "payload")


def _engine_event(row: dict[str, Any]) -> dict:
    p = row["payload"]
    if isinstance(p, str):
        p = json.loads(p)
    return {
        "event_id": row["event_id"],
        "ts": row["ts"],
        "seq": row["seq"],
        "action": row["action"],
        "payload": p,
    }


class MemoryRepo:
    """测试用内存仓储: 事件插入去重, 报告只追加。"""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.reports: list[dict] = []

    def init_schema(self) -> None:
        return None

    def get_existing_ids(self, ids: Sequence[str]) -> set[str]:
        return {i for i in ids if i in self.events}

    def insert_events(self, events: Sequence[dict]) -> int:
        n = 0
        for ev in events:
            if ev["event_id"] in self.events:
                continue
            self.events[ev["event_id"]] = dict(ev)
            n += 1
        return n

    def all_engine_events(self) -> list[dict]:
        return [_engine_event(e) for e in self.events.values()]

    def save_report(self, body: dict, signature: str, change_reason: str) -> int:
        version = len(self.reports) + 1
        self.reports.append(
            {
                "version": version,
                "hash": body["hash"],
                "body": body,
                "signature": signature,
                "change_reason": change_reason,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return version

    def get_report(self, version: int) -> dict | None:
        if 1 <= version <= len(self.reports):
            return self.reports[version - 1]
        return None

    def get_latest_version(self) -> int | None:
        return len(self.reports) or None

    def list_reports(self, limit: int = 50) -> list[dict]:
        out = []
        for r in self.reports[-limit:][::-1]:
            out.append(
                {
                    "version": r["version"],
                    "created_at": r["created_at"],
                    "hash": r["hash"],
                    "event_count": r["body"]["event_count"],
                    "as_of": r["body"]["as_of"],
                    "change_reason": r["change_reason"],
                    "signature": r["signature"],
                }
            )
        return out

    def ping(self) -> bool:
        return True


class PostgresRepo:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    @contextmanager
    def _conn(self):
        conn = psycopg.connect(self.conninfo, row_factory=dict_row)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute(SCHEMA_SQL)

    def ping(self) -> bool:
        with self._conn() as conn:
            conn.execute("SELECT 1")
        return True

    def get_existing_ids(self, ids: Sequence[str]) -> set[str]:
        if not ids:
            return set()
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_id FROM events WHERE event_id = ANY(%s)",
                (list(ids),),
            ).fetchall()
        return {r["event_id"] for r in rows}

    def insert_events(self, events: Sequence[dict]) -> int:
        if not events:
            return 0
        rows = [
            (
                e["event_id"],
                e["ts"],
                e["seq"],
                e["action"],
                json.dumps(e["payload"], sort_keys=True, ensure_ascii=False),
                e["signer"],
                e["signature"],
            )
            for e in events
        ]
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO events (event_id, ts, seq, action, payload, signer, signature)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    """,
                    rows,
                )
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def all_engine_events(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT event_id, ts, seq, action, payload FROM events"
            ).fetchall()
        return [_engine_event(r) for r in rows]

    def save_report(self, body: dict, signature: str, change_reason: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                """
                INSERT INTO reports (hash, body, signature, change_reason)
                VALUES (%s, %s::jsonb, %s, %s) RETURNING version
                """,
                (
                    body["hash"],
                    json.dumps(body, sort_keys=True, ensure_ascii=False),
                    signature,
                    change_reason,
                ),
            ).fetchone()
        return int(row["version"])

    def get_report(self, version: int) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT version, created_at, hash, body, signature, change_reason "
                "FROM reports WHERE version = %s",
                (version,),
            ).fetchone()
        if row is None:
            return None
        body = row["body"]
        if isinstance(body, str):
            body = json.loads(body)
        return {
            "version": row["version"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "hash": row["hash"],
            "body": body,
            "signature": row["signature"],
            "change_reason": row["change_reason"],
        }

    def get_latest_version(self) -> int | None:
        with self._conn() as conn:
            row = conn.execute("SELECT max(version) AS v FROM reports").fetchone()
        return None if row["v"] is None else int(row["v"])

    def list_reports(self, limit: int = 50) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT version, created_at, hash, body, signature, change_reason "
                "FROM reports ORDER BY version DESC LIMIT %s",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            body = row["body"]
            if isinstance(body, str):
                body = json.loads(body)
            out.append(
                {
                    "version": row["version"],
                    "created_at": row["created_at"].isoformat()
                    if row["created_at"]
                    else None,
                    "hash": row["hash"],
                    "event_count": body["event_count"],
                    "as_of": body["as_of"],
                    "change_reason": row["change_reason"],
                    "signature": row["signature"],
                }
            )
        return out
