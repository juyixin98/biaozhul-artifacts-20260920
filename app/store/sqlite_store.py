"""SQLite-backed evidence store.

The store is a *durable record* of evaluation, never the inference engine:
recursion and joins happen in :mod:`app.engine`.  After a fixpoint is
computed in memory, every EDB fact and every derived tuple (with its
positive support and negation checks) is persisted here, keyed by

* **program version** — SHA-256 fingerprint of the exact source program;
* **request id** — caller-supplied or generated, correlating API calls,
  stored rows, and log lines.

Schema (normalised enough to answer provenance questions in SQL)::

    programs          program_id, version, source, created_at
    program_rules     program_id, rule_index, text
    requests          request_id, program_id, version, kind, goal,
                      status, answer_count, uncertainty, message, created_at
    derivations       id, request_id, program_id, version,
                      pred, arity, tuple_text, kind, rule_index
    derivation_support derivation_id, position, kind ('pos'|'neg'),
                      pred, arity, tuple_text

``tuple_text`` is the canonical ``json`` array encoding of a ground tuple.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..engine.fixpoint import FixpointResult
from ..language.terms import render_tuple
from ..engine.relations import PredKey, Tuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS programs (
    program_id   TEXT PRIMARY KEY,
    version      TEXT NOT NULL,
    source       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS program_rules (
    program_id   TEXT NOT NULL,
    rule_index   INTEGER NOT NULL,
    text         TEXT NOT NULL,
    PRIMARY KEY (program_id, rule_index)
);
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    program_id   TEXT,
    version      TEXT,
    kind         TEXT NOT NULL,
    goal         TEXT,
    status       TEXT NOT NULL,
    answer_count INTEGER NOT NULL DEFAULT 0,
    uncertainty  INTEGER NOT NULL DEFAULT 0,
    message      TEXT,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS derivations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id   TEXT NOT NULL,
    program_id   TEXT NOT NULL,
    version      TEXT NOT NULL,
    pred         TEXT NOT NULL,
    arity        INTEGER NOT NULL,
    tuple_text   TEXT NOT NULL,
    kind         TEXT NOT NULL,
    rule_index   INTEGER
);
CREATE TABLE IF NOT EXISTS derivation_support (
    derivation_id INTEGER NOT NULL,
    position      INTEGER NOT NULL,
    kind          TEXT NOT NULL,
    pred          TEXT NOT NULL,
    arity         INTEGER NOT NULL,
    tuple_text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_deriv_req ON derivations(request_id);
CREATE INDEX IF NOT EXISTS idx_deriv_ver ON derivations(version, pred);
CREATE INDEX IF NOT EXISTS idx_support_deriv ON derivation_support(derivation_id);
"""


def encode_tuple(tup: Tuple) -> str:
    return json.dumps(list(tup), ensure_ascii=False)


def decode_tuple(text: str) -> Tuple:
    return tuple(json.loads(text))


def new_request_id() -> str:
    return f"req_{uuid.uuid4().hex[:16]}"


class EvidenceStore:
    """Thread-safe wrapper around a single SQLite file.

    A lock serialises writers; the workload (small synthetic fixtures) does
    not need connection pooling.  ``:memory:`` databases are supported for
    tests.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # check_same_thread=False + an explicit write lock; FastAPI may run
        # sync endpoints on a threadpool.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------

    def save_program(self, *, program_id: str, version: str, source: str, rule_texts: list[str]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO programs(program_id, version, source, created_at) VALUES (?,?,?,?)",
                (program_id, version, source, _now()),
            )
            for i, text in enumerate(rule_texts):
                self._conn.execute(
                    "INSERT OR REPLACE INTO program_rules(program_id, rule_index, text) VALUES (?,?,?)",
                    (program_id, i, text),
                )
            self._conn.commit()

    def record_request(
        self,
        *,
        request_id: str,
        kind: str,
        status: str,
        program_id: str | None = None,
        version: str | None = None,
        goal: str | None = None,
        answer_count: int = 0,
        uncertainty: bool = False,
        message: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO requests
                   (request_id, program_id, version, kind, goal, status,
                    answer_count, uncertainty, message, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    request_id,
                    program_id,
                    version,
                    kind,
                    goal,
                    status,
                    answer_count,
                    1 if uncertainty else 0,
                    message,
                    _now(),
                ),
            )
            self._conn.commit()

    def persist_fixpoint(self, request_id: str, program_id: str, version: str, result: FixpointResult) -> int:
        """Persist every fact/derived tuple of a fixpoint. Returns row count."""
        count = 0
        with self._lock:
            for key, relation in sorted(result.db.rels.items()):
                for tup in sorted(relation):
                    is_base = (key, tup) in result.base_facts
                    firing = None if is_base else result.evidence.get((key, tup))
                    cur = self._conn.execute(
                        """INSERT INTO derivations
                           (request_id, program_id, version, pred, arity,
                            tuple_text, kind, rule_index)
                           VALUES (?,?,?,?,?,?,?,?)""",
                        (
                            request_id,
                            program_id,
                            version,
                            key[0],
                            key[1],
                            encode_tuple(tup),
                            "edb" if is_base else "idb",
                            None if firing is None else firing.rule_index,
                        ),
                    )
                    deriv_id = cur.lastrowid
                    if firing is not None:
                        position = 0
                        for child_key, child_tup in firing.pos_inputs:
                            self._insert_support(deriv_id, position, "pos", child_key, child_tup)
                            position += 1
                        for neg_key, pattern in firing.neg_checks:
                            self._insert_support(deriv_id, position, "neg", neg_key, pattern)
                            position += 1
                    count += 1
            self._conn.commit()
        return count

    def _insert_support(
        self,
        deriv_id: int,
        position: int,
        kind: str,
        key: PredKey,
        tup: Tuple,
    ) -> None:
        self._conn.execute(
            """INSERT INTO derivation_support
               (derivation_id, position, kind, pred, arity, tuple_text)
               VALUES (?,?,?,?,?,?)""",
            (deriv_id, position, kind, key[0], key[1], encode_tuple(tup)),
        )

    # -- read side / explainability ------------------------------------

    def get_request(self, request_id: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_derivation(self, request_id: str, pred: str, tup: Tuple) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM derivations
                   WHERE request_id = ? AND pred = ? AND tuple_text = ?""",
                (request_id, pred, encode_tuple(tup)),
            ).fetchone()
            if row is None:
                return None
            out = dict(row)
            out["support"] = [
                dict(r)
                for r in self._conn.execute(
                    "SELECT * FROM derivation_support WHERE derivation_id = ? ORDER BY position",
                    (row["id"],),
                ).fetchall()
            ]
            return out

    def request_summary(self, request_id: str) -> dict:
        with self._lock:
            req = self.get_request(request_id)
            rows = self._conn.execute(
                """SELECT pred, arity, kind, COUNT(*) AS n
                   FROM derivations WHERE request_id = ?
                   GROUP BY pred, arity, kind ORDER BY pred, kind""",
                (request_id,),
            ).fetchall()
            return {
                "request": req,
                "relations": [
                    {"predicate": f"{r['pred']}/{r['arity']}", "kind": r["kind"], "count": r["n"]}
                    for r in rows
                ],
            }


def render_ground(key: PredKey, tup: Tuple) -> str:
    return f"{key[0]}{render_tuple(tup)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
