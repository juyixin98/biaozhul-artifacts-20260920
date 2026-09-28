"""SQLite-backed evidence and audit store.

The store is deliberately *not* used to compute fixpoints: the inference
engine evaluates rules in Python.  SQLite is the durable, inspectable
record of:

* immutable program snapshots (rules + rule version),
* fact sets and their content hash (fact-set semantics),
* materializations with per-stratum round counts and trace,
* every derived tuple, its rule id, round and body bindings,
* one audit row per API request correlated by request id.

Snapshots are content-addressed: posting the same rules and facts twice
returns the existing program/materialization ids instead of duplicating.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..engine.derivations import Derivation
from ..engine.fixpoint import Materialization
from ..engine.relational import Row
from ..language.compiler import CompiledProgram
from ..language.ast import Atom

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    program_id        TEXT PRIMARY KEY,
    rule_version      TEXT NOT NULL,
    fact_set_version  TEXT NOT NULL,
    normalized_text   TEXT NOT NULL,
    fact_count        INTEGER NOT NULL,
    rule_count        INTEGER NOT NULL,
    created_at        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rules (
    program_id    TEXT NOT NULL REFERENCES programs(program_id),
    rule_id       TEXT NOT NULL,
    rule_hash     TEXT NOT NULL,
    stratum       INTEGER NOT NULL,
    text          TEXT NOT NULL,
    PRIMARY KEY (program_id, rule_id)
);
CREATE TABLE IF NOT EXISTS facts (
    program_id    TEXT NOT NULL REFERENCES programs(program_id),
    predicate     TEXT NOT NULL,
    row_json      TEXT NOT NULL,
    canonical     TEXT NOT NULL,
    PRIMARY KEY (program_id, predicate, row_json)
);
CREATE TABLE IF NOT EXISTS materializations (
    materialization_id TEXT PRIMARY KEY,
    program_id         TEXT NOT NULL REFERENCES programs(program_id),
    rule_version       TEXT NOT NULL,
    fact_set_version   TEXT NOT NULL,
    strata_rounds      TEXT NOT NULL,
    stats              TEXT NOT NULL,
    trace              TEXT NOT NULL,
    evaluated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tuples (
    materialization_id TEXT NOT NULL REFERENCES materializations(materialization_id),
    predicate          TEXT NOT NULL,
    row_json           TEXT NOT NULL,
    kind               TEXT NOT NULL,
    rule_id            TEXT,
    round              INTEGER,
    stratum            INTEGER,
    bindings_json      TEXT,
    PRIMARY KEY (materialization_id, predicate, row_json)
);
CREATE TABLE IF NOT EXISTS request_log (
    request_id          TEXT PRIMARY KEY,
    program_id          TEXT,
    materialization_id  TEXT,
    endpoint            TEXT NOT NULL,
    goal                TEXT,
    status              TEXT NOT NULL,
    http_status         INTEGER NOT NULL,
    result_count        INTEGER,
    error_code          TEXT,
    error_message       TEXT,
    details_json        TEXT,
    created_at          REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tuples_pred ON tuples(materialization_id, predicate);
CREATE INDEX IF NOT EXISTS idx_requests_created ON request_log(created_at);
"""


def row_to_json(row: Row) -> str:
    return json.dumps(list(row), ensure_ascii=False, separators=(",", ":"))


def json_to_row(text: str) -> Row:
    return tuple(json.loads(text))


def _rule_rows(program_id: str, compiled: CompiledProgram):
    return [
        (
            program_id,
            cr.rule_id,
            cr.rule_hash,
            compiled.predicates[cr.head_predicate].stratum,
            cr.text,
        )
        for cr in compiled.rules
    ]


def _fact_rows(program_id: str, facts: Sequence[Atom]):
    return [
        (
            program_id,
            f.predicate,
            row_to_json(tuple(t.value for t in f.args)),  # type: ignore[union-attr]
            f.canonical(),
        )
        for f in facts
    ]


def _materialization_params(mat: Materialization, program_id: str):
    stats = [
        {"stratum": s.stratum, "rounds": s.rounds, "derived": s.derived}
        for s in mat.stats
    ]
    return (
        mat.materialization_id,
        program_id,
        mat.compiled.rule_version,
        mat.fact_set_version,
        json.dumps(list(mat.strata_rounds)),
        json.dumps(stats),
        json.dumps([t.to_dict() for t in mat.trace]),
        mat.evaluated_at,
    )


def _bindings_json(derivation: Derivation) -> str:
    return json.dumps(
        [
            {"predicate": b.predicate, "row": list(b.row), "negated": b.negated}
            for b in derivation.bindings
        ]
    )


def _tuple_row(materialization_id: str, key, derivation: Derivation):
    predicate, row = key
    if derivation.is_fact:
        return (
            materialization_id, predicate, row_to_json(row),
            "fact", None, 0, derivation.stratum, None,
        )
    return (
        materialization_id, predicate, row_to_json(row),
        "derived", derivation.rule_id, derivation.round_no,
        derivation.stratum, _bindings_json(derivation),
    )


class EvidenceStore:
    def __init__(self, path: str = ":memory:"):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # Programs / materializations
    # ------------------------------------------------------------------ #

    def find_program(self, rule_version: str, fact_set_version: str) -> Optional[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT program_id FROM programs "
                "WHERE rule_version = ? AND fact_set_version = ?",
                (rule_version, fact_set_version),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def save_program(
        self,
        program_id: str,
        compiled: CompiledProgram,
        facts: Sequence[Atom],
        fact_set_version: str,
    ) -> bool:
        """Insert a program snapshot. Returns False if it already exists."""
        with self._lock:
            existing = self.find_program(compiled.rule_version, fact_set_version)
            if existing is not None:
                return False
            self._conn.execute(
                "INSERT INTO programs VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    program_id, compiled.rule_version, fact_set_version,
                    compiled.normalized_text, len(facts), len(compiled.rules),
                    time.time(),
                ),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO rules VALUES (?, ?, ?, ?, ?)",
                _rule_rows(program_id, compiled),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO facts VALUES (?, ?, ?, ?)",
                _fact_rows(program_id, facts),
            )
            return True

    def find_materialization(self, program_id: str) -> Optional[str]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT materialization_id FROM materializations WHERE program_id = ?",
                (program_id,),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def save_materialization(self, mat: Materialization, program_id: str) -> bool:
        with self._lock:
            if self.find_materialization(program_id) is not None:
                return False
            self._conn.execute(
                "INSERT INTO materializations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                _materialization_params(mat, program_id),
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO tuples VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [_tuple_row(mat.materialization_id, key, derivation)
                 for key, derivation in mat.derivations.items()],
            )
            return True

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #

    def get_program(self, program_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM programs WHERE program_id = ?", (program_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def get_materialization_record(self, materialization_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM materializations WHERE materialization_id = ?",
                (materialization_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def list_tuple_rows(
        self, materialization_id: str, predicate: str
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT predicate, row_json, kind, rule_id, round, stratum "
                "FROM tuples WHERE materialization_id = ? AND predicate = ? "
                "ORDER BY row_json",
                (materialization_id, predicate),
            )
            return [dict(r) for r in cur.fetchall()]

    # ------------------------------------------------------------------ #
    # Request audit log
    # ------------------------------------------------------------------ #

    def log_request(
        self,
        *,
        request_id: str,
        endpoint: str,
        status: str,
        http_status: int,
        program_id: Optional[str] = None,
        materialization_id: Optional[str] = None,
        goal: Optional[str] = None,
        result_count: Optional[int] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO request_log VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    request_id, program_id, materialization_id, endpoint, goal,
                    status, http_status, result_count, error_code, error_message,
                    json.dumps(details, ensure_ascii=False) if details else None,
                    time.time(),
                ),
            )

    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM request_log WHERE request_id = ?", (request_id,)
            )
            row = cur.fetchone()
            if not row:
                return None
            out = dict(row)
            if out.get("details_json"):
                out["details"] = json.loads(out.pop("details_json"))
            else:
                out.pop("details_json", None)
            return out
