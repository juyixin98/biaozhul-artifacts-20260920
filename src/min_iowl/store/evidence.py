"""SQLite-backed evidence store.

Everything needed to *re-audit* a result is persisted:

* ontologies and every axiom with its stable id (the declared **sources**);
* one row per reasoning run: engine version, timing, consistency state and the
  full JSON evidence (types, proof trees, conflict paths);
* a structured request log keyed by request id, so API responses, log lines and
  stored evidence are joinable on one correlation id.

Connections are opened per call (short-lived local service) and WAL is enabled.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

__all__ = ["EvidenceStore", "AxiomRow"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ontologies (
    ontology_id TEXT PRIMARY KEY,
    label       TEXT,
    created_at  TEXT NOT NULL,
    axiom_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS axioms (
    ontology_id     TEXT NOT NULL REFERENCES ontologies(ontology_id),
    axiom_id        TEXT NOT NULL,
    idx             INTEGER NOT NULL,
    kind            TEXT NOT NULL,
    functional_text TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    PRIMARY KEY (ontology_id, axiom_id)
);

CREATE TABLE IF NOT EXISTS reasoning_runs (
    run_id          TEXT PRIMARY KEY,
    ontology_id     TEXT NOT NULL REFERENCES ontologies(ontology_id),
    created_at      TEXT NOT NULL,
    engine_version  TEXT NOT NULL,
    duration_ms     REAL NOT NULL,
    inconsistent    INTEGER NOT NULL,
    unsatisfiable   TEXT NOT NULL,
    report_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS request_log (
    request_id  TEXT NOT NULL,
    ts          TEXT NOT NULL,
    method      TEXT NOT NULL,
    path        TEXT NOT NULL,
    status_code INTEGER,
    stage       TEXT NOT NULL,
    detail      TEXT

);

CREATE INDEX IF NOT EXISTS idx_request_log_req ON request_log(request_id);
CREATE INDEX IF NOT EXISTS idx_runs_onto ON reasoning_runs(ontology_id);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class AxiomRow:
    __slots__ = ("axiom_id", "idx", "kind", "functional_text", "raw_json")

    def __init__(self, axiom_id: str, idx: int, kind: str, functional_text: str, raw_json: str) -> None:
        self.axiom_id = axiom_id
        self.idx = idx
        self.kind = kind
        self.functional_text = functional_text
        self.raw_json = raw_json


class EvidenceStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self.init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    # --------------------------------------------------------------- ontologies
    def ontology_exists(self, ontology_id: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM ontologies WHERE ontology_id = ?", (ontology_id,)
            ).fetchone()
            return row is not None

    def save_ontology(
        self,
        ontology_id: str,
        label: str | None,
        axioms: list[tuple[str, int, str, str, dict[str, Any]]],
    ) -> None:
        """``axioms`` items: (axiom_id, index, kind, functional_text, raw_json)."""
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO ontologies(ontology_id, label, created_at, axiom_count) VALUES (?,?,?,?)",
                (ontology_id, label, _utcnow(), len(axioms)),
            )
            conn.executemany(
                "INSERT INTO axioms(ontology_id, axiom_id, idx, kind, functional_text, raw_json)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (ontology_id, aid, idx, kind, text, json.dumps(raw, ensure_ascii=False))
                    for aid, idx, kind, text, raw in axioms
                ],
            )

    def list_ontologies(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ontology_id, label, created_at, axiom_count"
                " FROM ontologies ORDER BY created_at"
            ).fetchall()
            return [dict(r) for r in rows]

    def load_axioms(self, ontology_id: str) -> list[AxiomRow]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT axiom_id, idx, kind, functional_text, raw_json"
                " FROM axioms WHERE ontology_id = ? ORDER BY idx",
                (ontology_id,),
            ).fetchall()
            return [
                AxiomRow(r["axiom_id"], r["idx"], r["kind"], r["functional_text"], r["raw_json"])
                for r in rows
            ]

    # -------------------------------------------------------------------- runs
    def save_run(
        self,
        run_id: str,
        ontology_id: str,
        engine_version: str,
        duration_ms: float,
        inconsistent: bool,
        unsatisfiable: list[str],
        report_json: dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO reasoning_runs(run_id, ontology_id, created_at, engine_version,"
                " duration_ms, inconsistent, unsatisfiable, report_json) VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    ontology_id,
                    _utcnow(),
                    engine_version,
                    duration_ms,
                    1 if inconsistent else 0,
                    json.dumps(unsatisfiable, ensure_ascii=False),
                    json.dumps(report_json, ensure_ascii=False),
                ),
            )

    def list_runs(self, ontology_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT run_id, created_at, engine_version, duration_ms, inconsistent,"
                " unsatisfiable FROM reasoning_runs WHERE ontology_id = ? ORDER BY created_at",
                (ontology_id,),
            ).fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                d["inconsistent"] = bool(d["inconsistent"])
                d["unsatisfiable"] = json.loads(d["unsatisfiable"])
                out.append(d)
            return out

    def load_run_report(self, run_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT report_json FROM reasoning_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return json.loads(row["report_json"]) if row else None

    # ------------------------------------------------------------- request log
    def log_request(
        self,
        request_id: str,
        method: str,
        path: str,
        stage: str,
        status_code: int | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO request_log(request_id, ts, method, path, status_code, stage, detail)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    request_id,
                    _utcnow(),
                    method,
                    path,
                    status_code,
                    stage,
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )

    def request_events(self, request_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, method, path, status_code, stage, detail"
                " FROM request_log WHERE request_id = ? ORDER BY rowid",
                (request_id,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["detail"] = json.loads(d["detail"])
                out.append(d)
            return out
