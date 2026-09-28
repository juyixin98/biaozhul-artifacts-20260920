"""SQLite repository for corpora.

The corpus is stored relationally (corpora / sequences / events tables) rather
than as opaque JSON blobs, so symbol lookups and corpus listings can use SQL
indexes.  The repository maps rows back into the frozen domain models in
:mod:`app.models`.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.config import settings
from app.errors import ConflictingCorpusError, NotFoundError
from app.models import Corpus, Event, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    corpus_id    TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT,
    has_timestamps INTEGER NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sequences (
    corpus_id   TEXT NOT NULL REFERENCES corpora(corpus_id) ON DELETE CASCADE,
    seq_index   INTEGER NOT NULL,
    sequence_id TEXT NOT NULL,
    length      INTEGER NOT NULL,
    PRIMARY KEY (corpus_id, seq_index),
    UNIQUE (corpus_id, sequence_id)
);
CREATE TABLE IF NOT EXISTS events (
    corpus_id  TEXT NOT NULL,
    seq_index  INTEGER NOT NULL,
    position   INTEGER NOT NULL,
    symbol     TEXT NOT NULL,
    timestamp  REAL,
    PRIMARY KEY (corpus_id, seq_index, position)
);
CREATE INDEX IF NOT EXISTS idx_events_symbol
    ON events (corpus_id, symbol);
"""


class CorpusRepository:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.db_path
        parent = Path(self.db_path).parent
        parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ writes

    def save_corpus(self, corpus: Corpus, *, has_timestamps: bool) -> None:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM corpora WHERE corpus_id = ?", (corpus.corpus_id,)
            ).fetchone()
            if existing is not None:
                raise ConflictingCorpusError(
                    "corpus already exists",
                    details={"corpus_id": corpus.corpus_id},
                )
            conn.execute(
                "INSERT INTO corpora (corpus_id, name, description, has_timestamps) "
                "VALUES (?, ?, ?, ?)",
                (corpus.corpus_id, corpus.name, corpus.description,
                 1 if has_timestamps else 0),
            )
            for seq_index, seq in enumerate(corpus.sequences):
                conn.execute(
                    "INSERT INTO sequences (corpus_id, seq_index, sequence_id, length) "
                    "VALUES (?, ?, ?, ?)",
                    (corpus.corpus_id, seq_index, seq.sequence_id, len(seq.events)),
                )
                conn.executemany(
                    "INSERT INTO events (corpus_id, seq_index, position, symbol, timestamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (corpus.corpus_id, seq_index, e.position, e.symbol, e.timestamp)
                        for e in seq.events
                    ],
                )

    def delete_corpus(self, corpus_id: str) -> None:
        with self._connect() as conn:
            # Explicit cascade: ``events`` has no FK of its own, so delete
            # children before the parent, inside one transaction.
            exists = conn.execute(
                "SELECT 1 FROM corpora WHERE corpus_id = ?", (corpus_id,)
            ).fetchone()
            if exists is None:
                raise NotFoundError(
                    "corpus not found", details={"corpus_id": corpus_id}
                )
            conn.execute("DELETE FROM events WHERE corpus_id = ?", (corpus_id,))
            conn.execute("DELETE FROM sequences WHERE corpus_id = ?", (corpus_id,))
            conn.execute("DELETE FROM corpora WHERE corpus_id = ?", (corpus_id,))

    # ------------------------------------------------------------------- reads

    def list_corpora(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT c.corpus_id, c.name, c.description, c.has_timestamps, "
                "c.created_at, COUNT(s.seq_index) AS seq_count, "
                "COALESCE(SUM(s.length), 0) AS event_count "
                "FROM corpora c LEFT JOIN sequences s ON s.corpus_id = c.corpus_id "
                "GROUP BY c.corpus_id ORDER BY c.created_at"
            ).fetchall()
            return [dict(row) for row in rows]

    def get_corpus(self, corpus_id: str) -> Corpus:
        with self._connect() as conn:
            meta = conn.execute(
                "SELECT corpus_id, name, description FROM corpora WHERE corpus_id = ?",
                (corpus_id,),
            ).fetchone()
            if meta is None:
                raise NotFoundError(
                    "corpus not found", details={"corpus_id": corpus_id}
                )
            seq_rows = conn.execute(
                "SELECT seq_index, sequence_id FROM sequences "
                "WHERE corpus_id = ? ORDER BY seq_index",
                (corpus_id,),
            ).fetchall()
            sequences: list[Sequence] = []
            for seq_row in seq_rows:
                event_rows = conn.execute(
                    "SELECT position, symbol, timestamp FROM events "
                    "WHERE corpus_id = ? AND seq_index = ? ORDER BY position",
                    (corpus_id, seq_row["seq_index"]),
                ).fetchall()
                events = tuple(
                    Event(position=r["position"], symbol=r["symbol"],
                          timestamp=r["timestamp"])
                    for r in event_rows
                )
                sequences.append(Sequence(sequence_id=seq_row["sequence_id"],
                                          events=events))
            return Corpus(
                corpus_id=meta["corpus_id"],
                name=meta["name"],
                description=meta["description"],
                sequences=tuple(sequences),
            )

    def corpus_has_timestamps(self, corpus_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT has_timestamps FROM corpora WHERE corpus_id = ?",
                (corpus_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError(
                    "corpus not found", details={"corpus_id": corpus_id}
                )
            return bool(row["has_timestamps"])
