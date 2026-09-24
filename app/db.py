"""SQLite 持久层：存储上传的 SBOM、核对结果与每个组件的结论。

使用标准库 ``sqlite3``，结果 JSON 同时落表（便于按结论筛选）与
整体存档（便于原样取回）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sbom_runs (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    spec_version    TEXT NOT NULL,
    serial_number   TEXT,
    bom_version     INTEGER,
    source_sha256   TEXT NOT NULL,
    component_count INTEGER NOT NULL,
    affected_count  INTEGER NOT NULL,
    unknown_count   INTEGER NOT NULL,
    not_affected_count INTEGER NOT NULL,
    cycle_count     INTEGER NOT NULL,
    request_json    TEXT NOT NULL,
    result_json     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS component_findings (
    run_id       TEXT NOT NULL,
    component_key TEXT NOT NULL,
    purl         TEXT NOT NULL,
    ecosystem    TEXT NOT NULL,
    name         TEXT NOT NULL,
    version      TEXT,
    scope        TEXT NOT NULL,
    disposition  TEXT NOT NULL,  -- affected | unknown | not_affected
    is_transitive INTEGER NOT NULL,
    vulnerability_ids TEXT NOT NULL,  -- JSON array
    PRIMARY KEY (run_id, component_key),
    FOREIGN KEY (run_id) REFERENCES sbom_runs(id)
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def save_run(conn: sqlite3.Connection, *, run_id: str, created_at: str,
             report_meta: dict, source_sha256: str,
             component_count: int, result: dict,
             flat_components: list[dict]) -> None:
    """原子写入一次核对运行与其组件明细。"""
    try:
        with conn:  # 事务
            conn.execute(
                """INSERT INTO sbom_runs (id, created_at, spec_version,
                   serial_number, bom_version, source_sha256, component_count,
                   affected_count, unknown_count, not_affected_count,
                   cycle_count, request_json, result_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, created_at, report_meta["spec_version"],
                 report_meta.get("serial_number"),
                 report_meta.get("bom_version"), source_sha256,
                 component_count, result["summary"]["affected_components"],
                 result["summary"]["unknown_components"],
                 result["summary"]["not_affected_components"],
                 result["summary"]["cycles_detected"],
                 json.dumps(result["request"], ensure_ascii=False),
                 json.dumps(result, ensure_ascii=False)),
            )
            conn.executemany(
                """INSERT INTO component_findings (run_id, component_key, purl,
                   ecosystem, name, version, scope, disposition, is_transitive,
                   vulnerability_ids)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(run_id, c["component_key"], c["purl"], c["ecosystem"],
                  c["name"], c["version"], c["scope"], c["disposition"],
                  1 if c["is_transitive"] else 0,
                  json.dumps(c["vulnerability_ids"], ensure_ascii=False))
                 for c in flat_components],
            )
    except sqlite3.Error as exc:  # 明确抛出，由 API 层如实报 500
        raise RuntimeError(f"failed to persist run {run_id}: {exc}") from exc


def get_run(conn: sqlite3.Connection, run_id: str) -> dict | None:
    row = conn.execute(
        "SELECT result_json FROM sbom_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    return json.loads(row["result_json"])


def list_runs(conn: sqlite3.Connection, disposition: str | None = None,
              limit: int = 50) -> list[dict]:
    if disposition:
        rows = conn.execute(
            """SELECT DISTINCT r.id, r.created_at, r.spec_version,
                      r.serial_number, r.component_count, r.affected_count,
                      r.unknown_count, r.not_affected_count, r.cycle_count,
                      r.source_sha256
               FROM sbom_runs r
               JOIN component_findings c ON c.run_id = r.id
               WHERE c.disposition = ?
               ORDER BY r.created_at DESC, r.id DESC
               LIMIT ?""", (disposition, limit)).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, created_at, spec_version, serial_number,
                      component_count, affected_count, unknown_count,
                      not_affected_count, cycle_count, source_sha256
               FROM sbom_runs ORDER BY created_at DESC, id DESC LIMIT ?""",
            (limit,)).fetchall()
    return [dict(r) for r in rows]
