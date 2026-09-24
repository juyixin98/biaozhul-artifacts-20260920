"""SQLite persistence for submitted SBOMs and their findings.

Stores scans, normalized components and matched findings so that prior
results are queryable. The vulnerability fixture itself is loaded into
the ``advisories`` / ``affected_ranges`` tables at startup.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_WRITE_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id         TEXT NOT NULL UNIQUE,
    submitted_at    TEXT NOT NULL,
    document_ref    TEXT,
    spec_version    TEXT NOT NULL,
    input_sha256    TEXT NOT NULL,
    component_count INTEGER NOT NULL,
    findings_count  INTEGER NOT NULL,
    result_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id         TEXT NOT NULL,
    node_key        TEXT NOT NULL,
    bom_refs        TEXT NOT NULL,
    ecosystem       TEXT,
    name            TEXT,
    version         TEXT,
    scope           TEXT NOT NULL,
    relation        TEXT NOT NULL,
    merged_count    INTEGER NOT NULL,
    UNIQUE(scan_id, node_key)
);

CREATE TABLE IF NOT EXISTS advisories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vulnerability_id TEXT NOT NULL,
    ecosystem       TEXT NOT NULL,
    namespace       TEXT NOT NULL,
    name            TEXT NOT NULL,
    summary         TEXT,
    severity        TEXT,
    UNIQUE(vulnerability_id)
);

CREATE TABLE IF NOT EXISTS affected_ranges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    vulnerability_id TEXT NOT NULL,
    range_id        TEXT NOT NULL,
    ecosystem       TEXT NOT NULL,
    expression      TEXT NOT NULL,
    introduced      TEXT,
    fixed           TEXT,
    source_note     TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id         TEXT NOT NULL,
    node_key        TEXT NOT NULL,
    vulnerability_id TEXT,
    range_id        TEXT,
    status          TEXT NOT NULL,
    relation        TEXT,
    evidence_paths  TEXT,
    detail          TEXT
);

CREATE INDEX IF NOT EXISTS idx_findings_scan ON findings(scan_id);
CREATE INDEX IF NOT EXISTS idx_components_scan ON components(scan_id);
"""


def init_db(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection):
    with _WRITE_LOCK:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def replace_fixture(conn: sqlite3.Connection, advisories, fixture_meta: dict) -> None:
    with transaction(conn):
        conn.execute("DELETE FROM affected_ranges")
        conn.execute("DELETE FROM advisories")
        for adv in advisories:
            conn.execute(
                "INSERT INTO advisories (vulnerability_id, ecosystem, namespace, name, summary, severity)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (adv.vulnerability_id, adv.ecosystem, "/".join(adv.namespace),
                 adv.name, adv.summary, adv.severity),
            )
            for r in adv.ranges:
                conn.execute(
                    "INSERT INTO affected_ranges (vulnerability_id, range_id, ecosystem,"
                    " expression, introduced, fixed, source_note)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (adv.vulnerability_id, r.range_id, r.ecosystem, r.expression,
                     r.introduced, r.fixed, r.source_note),
                )
        conn.execute("CREATE TABLE IF NOT EXISTS fixture_meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("DELETE FROM fixture_meta")
        for k, v in fixture_meta.items():
            conn.execute("INSERT INTO fixture_meta (k, v) VALUES (?, ?)", (k, str(v)))


def save_scan(conn: sqlite3.Connection, *, scan_id: str, submitted_at: str,
              document_ref: str | None, spec_version: str, input_sha256: str,
              components: list[dict], result: dict) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO scans (scan_id, submitted_at, document_ref, spec_version,"
            " input_sha256, component_count, findings_count, result_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (scan_id, submitted_at, document_ref, spec_version, input_sha256,
             len(components),
             sum(1 for f in result["findings"] if f["status"] == "affected"),
             json.dumps(result, ensure_ascii=False)),
        )
        for c in components:
            conn.execute(
                "INSERT INTO components (scan_id, node_key, bom_refs, ecosystem, name,"
                " version, scope, relation, merged_count)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scan_id, c["node_key"], json.dumps(c["bom_refs"]),
                 c.get("ecosystem"), c.get("name"), c.get("version"),
                 c["scope"], c["relation"], c["merged_count"]),
            )
        for f in result["findings"]:
            conn.execute(
                "INSERT INTO findings (scan_id, node_key, vulnerability_id, range_id,"
                " status, relation, evidence_paths, detail)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (scan_id, f.get("node_key"), f["vulnerability_id"], f.get("range_id"),
                 f["status"], f.get("relation"),
                 json.dumps(f.get("evidence_paths", []), ensure_ascii=False),
                 f.get("detail")),
            )


def get_scan(conn: sqlite3.Connection, scan_id: str) -> dict | None:
    row = conn.execute("SELECT result_json FROM scans WHERE scan_id = ?", (scan_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def list_scans(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT scan_id, submitted_at, spec_version, component_count, findings_count"
        " FROM scans ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
