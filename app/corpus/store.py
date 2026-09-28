"""SQLite persistence: corpora, normalized event index, mining runs, results."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone

from app.corpus.schema import CorpusSpec
from app.models.domain import Event, PatternResult, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    corpus_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    digest      TEXT NOT NULL,
    spec_json   TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    corpus_id   TEXT NOT NULL,
    sequence_id TEXT NOT NULL,
    pos         INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    timestamp   REAL,
    PRIMARY KEY (corpus_id, sequence_id, pos)
);
CREATE INDEX IF NOT EXISTS idx_events_symbol ON events (corpus_id, symbol);
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    corpus_id     TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    status        TEXT NOT NULL,
    versions_json TEXT NOT NULL,
    pattern_count INTEGER,
    error         TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);
CREATE TABLE IF NOT EXISTS patterns (
    run_id       TEXT NOT NULL,
    pattern_json TEXT NOT NULL,
    support      INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (run_id, pattern_json)
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def corpus_digest(spec: CorpusSpec) -> str:
    canonical = json.dumps(spec.model_dump(), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def init_schema(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---- corpora -------------------------------------------------------

    def save_corpus(self, spec: CorpusSpec) -> tuple[str, str]:
        digest = corpus_digest(spec)
        corpus_id = uuid.uuid4().hex
        with self._conn:
            self._conn.execute(
                "INSERT INTO corpora VALUES (?, ?, ?, ?, ?)",
                (corpus_id, spec.name, digest, spec.model_dump_json(), _utcnow()),
            )
            self._conn.executemany(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
                [
                    (corpus_id, s.sequence_id, pos, e.symbol, e.timestamp)
                    for s in spec.sequences
                    for pos, e in enumerate(s.events)
                ],
            )
        return corpus_id, digest

    def corpus_exists(self, corpus_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM corpora WHERE corpus_id = ?", (corpus_id,)
        ).fetchone()
        return row is not None

    def get_corpus_spec(self, corpus_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT spec_json, digest FROM corpora WHERE corpus_id = ?", (corpus_id,)
        ).fetchone()
        if row is None:
            return None
        spec = json.loads(row["spec_json"])
        spec["digest"] = row["digest"]
        return spec

    def load_sequences(self, corpus_id: str) -> list[Sequence]:
        rows = self._conn.execute(
            "SELECT sequence_id, pos, symbol, timestamp FROM events "
            "WHERE corpus_id = ? ORDER BY sequence_id, pos",
            (corpus_id,),
        ).fetchall()
        grouped: dict[str, list[Event]] = {}
        for row in rows:
            grouped.setdefault(row["sequence_id"], []).append(
                Event(symbol=row["symbol"], timestamp=row["timestamp"])
            )
        return [
            Sequence(sequence_id=sid, events=tuple(events))
            for sid, events in grouped.items()
        ]

    # ---- runs ----------------------------------------------------------

    def create_run(self, corpus_id: str, params: dict, versions: dict) -> str:
        run_id = uuid.uuid4().hex
        with self._conn:
            self._conn.execute(
                "INSERT INTO runs (run_id, corpus_id, params_json, status,"
                " versions_json, started_at) VALUES (?, ?, ?, 'RUNNING', ?, ?)",
                (run_id, corpus_id, json.dumps(params), json.dumps(versions), _utcnow()),
            )
        return run_id

    def finish_run(
        self, run_id: str, status: str, pattern_count: int = 0, error: str | None = None
    ) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE runs SET status = ?, pattern_count = ?, error = ?,"
                " finished_at = ? WHERE run_id = ?",
                (status, pattern_count, error, _utcnow(), run_id),
            )

    def save_patterns(self, run_id: str, results: list[PatternResult]) -> None:
        with self._conn:
            self._conn.executemany(
                "INSERT INTO patterns VALUES (?, ?, ?, ?)",
                [
                    (
                        run_id,
                        json.dumps(list(r.pattern)),
                        r.support,
                        json.dumps(
                            [
                                {"sequence_id": e.sequence_id, "positions": list(e.positions)}
                                for e in r.embeddings
                            ]
                        ),
                    )
                    for r in results
                ],
            )

    def get_run(self, run_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_patterns(self, run_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT pattern_json, support, evidence_json FROM patterns"
            " WHERE run_id = ? ORDER BY pattern_json",
            (run_id,),
        ).fetchall()
        return [
            {
                "pattern": json.loads(r["pattern_json"]),
                "support": r["support"],
                "embeddings": json.loads(r["evidence_json"]),
            }
            for r in rows
        ]
