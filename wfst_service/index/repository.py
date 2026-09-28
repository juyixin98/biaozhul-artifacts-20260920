"""SQLite-backed repository for corpora, models and query runs."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..corpus.serialize import fst_from_dict, fst_to_dict
from ..core.fst import Fst
from .db import connect, init_schema


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


@dataclass(frozen=True, slots=True)
class StoredRun:
    run_id: str
    corpus_id: str
    target: str
    input: str
    k: int
    budget: int
    status: str
    complete: bool
    expansions: int
    error_code: str | None
    error_message: str | None
    started_at: str


class IndexRepository:
    """CRUD access; one transaction per mutating public call."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._path = db_path
        self._conn = connect(db_path)
        init_schema(self._conn)

    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Corpora
    # ------------------------------------------------------------------

    def list_corpora(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT corpus_id, description, loaded_at FROM corpora "
            "ORDER BY corpus_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_corpus(self, corpus_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM corpora WHERE corpus_id = ?", (corpus_id,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_corpus(
        self, corpus_id: str, description: str, spec_json: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO corpora(corpus_id, description, spec_json, "
                "loaded_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(corpus_id) DO UPDATE SET "
                "description=excluded.description, spec_json=excluded.spec_json, "
                "loaded_at=excluded.loaded_at",
                (corpus_id, description, spec_json, _now()),
            )
            self._conn.execute(
                "DELETE FROM transducers WHERE corpus_id = ?", (corpus_id,)
            )
            self._conn.execute(
                "DELETE FROM pipelines WHERE corpus_id = ?", (corpus_id,)
            )

    # ------------------------------------------------------------------
    # Transducers / composed pipelines
    # ------------------------------------------------------------------

    def put_transducer(
        self,
        corpus_id: str,
        name: str,
        kind: str,
        fst: Fst,
    ) -> None:
        doc = json.dumps(fst_to_dict(fst), ensure_ascii=False)
        with self._conn:
            self._conn.execute(
                "INSERT INTO transducers(corpus_id, name, kind, num_states, "
                "num_arcs, fst_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    corpus_id,
                    name,
                    kind,
                    fst.num_states,
                    len(fst.arcs),
                    doc,
                    _now(),
                ),
            )

    def get_transducer(self, corpus_id: str, name: str) -> Fst | None:
        row = self._conn.execute(
            "SELECT fst_json FROM transducers WHERE corpus_id = ? AND name = ?",
            (corpus_id, name),
        ).fetchone()
        return fst_from_dict(json.loads(row["fst_json"])) if row else None

    def list_transducers(self, corpus_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT name, kind, num_states, num_arcs FROM transducers "
            "WHERE corpus_id = ? ORDER BY name",
            (corpus_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def put_pipeline(
        self,
        corpus_id: str,
        name: str,
        sequence: tuple[str, ...],
        composed_name: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO pipelines(corpus_id, name, sequence_json, "
                "composed_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    corpus_id,
                    name,
                    json.dumps(list(sequence)),
                    composed_name,
                    _now(),
                ),
            )

    def get_pipeline(self, corpus_id: str, name: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM pipelines WHERE corpus_id = ? AND name = ?",
            (corpus_id, name),
        ).fetchone()
        return dict(row) if row else None

    def list_pipelines(self, corpus_id: str) -> dict[str, list[dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT name, sequence_json, composed_name FROM pipelines "
            "WHERE corpus_id = ? ORDER BY name",
            (corpus_id,),
        ).fetchall()
        return {
            row["name"]: {
                "sequence": json.loads(row["sequence_json"]),
                "composed_name": row["composed_name"],
            }
            for row in rows
        }

    # ------------------------------------------------------------------
    # Runs, results, logs
    # ------------------------------------------------------------------

    def start_run(
        self,
        run_id: str,
        corpus_id: str,
        target: str,
        text: str,
        k: int,
        budget: int,
        request_json: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs(run_id, corpus_id, target, input, k, budget, "
                "status, complete, expansions, request_json, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'running', 0, 0, ?, ?)",
                (
                    run_id,
                    corpus_id,
                    target,
                    text,
                    k,
                    budget,
                    request_json,
                    _now(),
                ),
            )

    def finish_run(
        self,
        run_id: str,
        status: str,
        complete: bool,
        expansions: int,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, complete = ?, expansions = ?, "
                "error_code = ?, error_message = ?, finished_at = ? "
                "WHERE run_id = ?",
                (
                    status,
                    1 if complete else 0,
                    expansions,
                    error_code,
                    error_message,
                    _now(),
                    run_id,
                ),
            )

    def add_result(self, run_id: str, rank: int, output: str, cost: float) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO results(run_id, rank, output, cost) "
                "VALUES (?, ?, ?, ?)",
                (run_id, rank, output, cost),
            )

    def add_logs(self, run_id: str, lines: list[str]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO run_logs(run_id, seq, line) VALUES (?, ?, ?)",
                [(run_id, seq, line) for seq, line in enumerate(lines)],
            )

    def get_run(self, run_id: str) -> StoredRun | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if not row:
            return None
        return StoredRun(
            run_id=row["run_id"],
            corpus_id=row["corpus_id"],
            target=row["target"],
            input=row["input"],
            k=row["k"],
            budget=row["budget"],
            status=row["status"],
            complete=bool(row["complete"]),
            expansions=row["expansions"],
            error_code=row["error_code"],
            error_message=row["error_message"],
            started_at=row["started_at"],
        )

    def get_results(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT rank, output, cost FROM results WHERE run_id = ? "
            "ORDER BY rank",
            (run_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_logs(self, run_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT seq, line FROM run_logs WHERE run_id = ? ORDER BY seq",
            (run_id,),
        ).fetchall()
        return [row["line"] for row in rows]
