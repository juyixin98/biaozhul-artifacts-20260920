"""SQLite-backed append-only evidence store."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..version import __version__

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    version      TEXT NOT NULL,
    settings     TEXT NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS rules (
    run_id       TEXT NOT NULL,
    rule_name    TEXT NOT NULL,
    salience     INTEGER NOT NULL,
    definition   TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    PRIMARY KEY (run_id, rule_name)
);

CREATE TABLE IF NOT EXISTS facts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    wme_id       INTEGER NOT NULL,
    op           TEXT NOT NULL CHECK (op IN ('insert', 'retract')),
    fact_type    TEXT NOT NULL,
    fields       TEXT NOT NULL,
    content_key  TEXT NOT NULL,
    origin       TEXT NOT NULL,
    duplicate    INTEGER NOT NULL DEFAULT 0,
    at_seq       INTEGER NOT NULL,
    at_ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    rule_name    TEXT NOT NULL,
    stable_key   TEXT NOT NULL,
    wme_ids      TEXT NOT NULL,
    bindings     TEXT NOT NULL,
    sources      TEXT NOT NULL,
    salience     INTEGER NOT NULL,
    seq          INTEGER NOT NULL,
    phase        TEXT NOT NULL CHECK (phase IN ('queued', 'invalidated', 'fired', 'suppressed')),
    at_ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS firings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    rule_name    TEXT NOT NULL,
    stable_key   TEXT NOT NULL,
    wme_ids      TEXT NOT NULL,
    bindings     TEXT NOT NULL,
    sources      TEXT NOT NULL,
    asserted     TEXT NOT NULL,
    retracted    TEXT NOT NULL,
    stopped      INTEGER NOT NULL,
    fire_order   INTEGER NOT NULL,
    at_ts        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    seq          INTEGER NOT NULL,
    ts           TEXT NOT NULL,
    level        TEXT NOT NULL,
    event        TEXT NOT NULL,
    payload      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_run ON facts(run_id, at_seq);
CREATE INDEX IF NOT EXISTS idx_act_run ON activations(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_fire_run ON firings(run_id, fire_order);
CREATE INDEX IF NOT EXISTS idx_trace_run ON trace(run_id, seq);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class EvidenceStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(SCHEMA)

    # -- helpers -------------------------------------------------------------

    # -- writes --------------------------------------------------------------

    @staticmethod
    def _dumps(payload: Any) -> str:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)

    def start_run(self, run_id: str, settings: dict[str, Any], note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO runs(run_id, started_at, version, settings, note) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, utc_now_iso(), __version__, self._dumps(settings), note),
            )

    def record_rules(self, run_id: str, compiled_rules: list[Any]) -> None:
        with self._lock:
            for seq, rule in enumerate(compiled_rules):
                definition = {
                    "name": rule.name,
                    "salience": rule.salience,
                    "refraction": rule.refraction,
                    "plans": [
                        {
                            "ce_index": p.ce_index,
                            "type": p.fact_type,
                            "tests": list(p.tests),
                            "binds": p.binds,
                            "requires": sorted(p.requires),
                        }
                        for p in rule.plans
                    ],
                    "action": rule.action,
                }
                self._conn.execute(
                    "INSERT OR REPLACE INTO rules(run_id, rule_name, salience, definition, seq) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (run_id, rule.name, rule.salience, self._dumps(definition), seq),
                )

    def record_fact(
        self,
        run_id: str,
        wme_id: int,
        op: str,
        fact_type: str,
        fields: dict[str, Any],
        content_key: str,
        origin: str,
        *,
        duplicate: bool = False,
        at_seq: int | None = None,
    ) -> int:
        seq = self.next_fact_seq(run_id) if at_seq is None else at_seq
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO facts(run_id, wme_id, op, fact_type, fields, content_key, "
                "origin, duplicate, at_seq, at_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, wme_id, op, fact_type, self._dumps(fields), content_key,
                    origin, int(duplicate), seq, utc_now_iso(),
                ),
            )
            return int(cur.lastrowid)

    def next_fact_seq(self, run_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(at_seq), 0) AS m FROM facts WHERE run_id = ?", (run_id,)
            ).fetchone()
            return int(row["m"]) + 1

    def record_activation(
        self,
        run_id: str,
        phase: str,
        *,
        rule_name: str,
        stable_key: str,
        wme_ids: list[int],
        bindings: dict[str, Any],
        sources: list[dict[str, Any]],
        salience: int,
        seq: int,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO activations(run_id, rule_name, stable_key, wme_ids, bindings, "
                "sources, salience, seq, phase, at_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, rule_name, stable_key, self._dumps(list(wme_ids)),
                    self._dumps(bindings), self._dumps(sources), salience, seq, phase,
                    utc_now_iso(),
                ),
            )

    def record_firing(self, run_id: str, record: Any, fire_order: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO firings(run_id, rule_name, stable_key, wme_ids, bindings, "
                "sources, asserted, retracted, stopped, fire_order, at_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_id, record.rule, record.stable_key, self._dumps(record.wme_ids),
                    self._dumps(record.bindings), self._dumps(record.sources),
                    self._dumps(record.asserted), self._dumps(record.retracted),
                    int(record.stopped), fire_order, utc_now_iso(),
                ),
            )

    def append_trace(
        self, run_id: str, seq: int, ts: str, level: str, event: str, payload: dict[str, Any]
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO trace(run_id, seq, ts, level, event, payload) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, seq, ts, level, event, self._dumps(payload)),
            )

    # -- reads ---------------------------------------------------------------

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "started_at": row["started_at"],
            "version": row["version"],
            "settings": json.loads(row["settings"]),
            "note": row["note"],
        }

    def list_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT run_id, started_at, version, note FROM runs ORDER BY started_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_trace(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, level, event, payload FROM trace WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [
            {"run_id": run_id, "seq": r["seq"], "ts": r["ts"], "level": r["level"],
             "event": r["event"], **json.loads(r["payload"])}
            for r in rows
        ]

    def get_evidence_summary(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            facts = self._conn.execute(
                "SELECT op, COUNT(*) AS c FROM facts WHERE run_id = ? GROUP BY op", (run_id,)
            ).fetchall()
            acts = self._conn.execute(
                "SELECT phase, COUNT(*) AS c FROM activations WHERE run_id = ? GROUP BY phase",
                (run_id,),
            ).fetchall()
            fires = self._conn.execute(
                "SELECT COUNT(*) AS c FROM firings WHERE run_id = ?", (run_id,)
            ).fetchone()
            trace_rows = self._conn.execute(
                "SELECT level, COUNT(*) AS c FROM trace WHERE run_id = ? GROUP BY level", (run_id,)
            ).fetchall()
        return {
            "facts": {r["op"]: r["c"] for r in facts},
            "activations": {r["phase"]: r["c"] for r in acts},
            "firings": fires["c"],
            "trace": {r["level"]: r["c"] for r in trace_rows},
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
