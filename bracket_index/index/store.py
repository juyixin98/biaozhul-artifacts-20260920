"""SQLite persistence for documents and chunk summaries.

Schema:
- documents: one row per document, with a monotonically increasing `version`
  used to reject stale edits.
- chunks: one row per chunk. Chunk text is stored so that a local edit only
  re-lexes the affected region; summaries are stored as JSON of tokens with
  chunk-relative positions, so shifting offsets after an edit never requires
  re-lexing untouched chunks.

All queries are parameterized; no string interpolation of values.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from ..kernel.lexer import Token
from ..kernel.summary import Mismatch, Summary

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL,
    length INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    doc_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    length INTEGER NOT NULL,
    text TEXT NOT NULL,
    entry_quote TEXT,
    escape_pending INTEGER NOT NULL DEFAULT 0,
    closers TEXT NOT NULL,
    openers TEXT NOT NULL,
    mismatches TEXT NOT NULL,
    PRIMARY KEY (doc_id, seq)
);
"""


@dataclass(frozen=True)
class DocumentRow:
    id: int
    version: int
    length: int


@dataclass(frozen=True)
class ChunkRow:
    seq: int
    length: int
    text: str
    entry_quote: str | None  # quote state entering the chunk; None = outside
    escape_pending: bool  # first char is escaped by a backslash in prev chunk
    summary: Summary  # positions are chunk-relative


def _token_to_json(tok: Token) -> list:
    return [tok.kind, tok.btype, tok.pos]


def _token_from_json(raw: list) -> Token:
    return Token(kind=raw[0], btype=raw[1], pos=raw[2])


def _summary_to_json(summary: Summary) -> tuple[str, str, str]:
    closers = json.dumps([_token_to_json(t) for t in summary.closers])
    openers = json.dumps([_token_to_json(t) for t in summary.openers])
    mismatches = json.dumps(
        [
            {"open": _token_to_json(m.open), "close": _token_to_json(m.close)}
            for m in summary.mismatches
        ]
    )
    return closers, openers, mismatches


def _summary_from_json(closers: str, openers: str, mismatches: str) -> Summary:
    return Summary(
        closers=tuple(_token_from_json(t) for t in json.loads(closers)),
        openers=tuple(_token_from_json(t) for t in json.loads(openers)),
        mismatches=tuple(
            Mismatch(
                open=_token_from_json(m["open"]),
                close=_token_from_json(m["close"]),
            )
            for m in json.loads(mismatches)
        ),
    )


class Store:
    def __init__(self, db_path: str) -> None:
        # check_same_thread=False because FastAPI runs sync routes in a
        # worker thread; an RLock serializes access to the single connection.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(chunks)").fetchall()
        }
        if "entry_quote" not in columns:
            self._conn.execute(
                "ALTER TABLE chunks ADD COLUMN entry_quote TEXT"
            )
        if "escape_pending" not in columns:
            self._conn.execute(
                "ALTER TABLE chunks ADD COLUMN escape_pending"
                " INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- documents ---------------------------------------------------------

    def create_document(self, length: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO documents (version, length, created_at)"
                " VALUES (?, ?, ?)",
                (0, length, datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def get_document(self, doc_id: int) -> DocumentRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, version, length FROM documents WHERE id = ?",
                (doc_id,),
            ).fetchone()
        if row is None:
            return None
        return DocumentRow(id=row["id"], version=row["version"], length=row["length"])

    def update_document_state(self, doc_id: int, version: int, length: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE documents SET version = ?, length = ? WHERE id = ?",
                (version, length, doc_id),
            )

    # -- chunks --------------------------------------------------------------

    def insert_chunk(
        self,
        doc_id: int,
        seq: int,
        text: str,
        entry_quote: str | None,
        escape_pending: bool,
        summary: Summary,
    ) -> None:
        with self._lock:
            closers, openers, mismatches = _summary_to_json(summary)
            self._conn.execute(
                "INSERT INTO chunks (doc_id, seq, length, text, entry_quote,"
                " escape_pending, closers, openers, mismatches)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id,
                    seq,
                    len(text),
                    text,
                    entry_quote,
                    1 if escape_pending else 0,
                    closers,
                    openers,
                    mismatches,
                ),
            )

    def get_chunks(self, doc_id: int) -> list[ChunkRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, length, text, entry_quote, escape_pending, closers,"
                " openers, mismatches FROM chunks WHERE doc_id = ? ORDER BY seq",
                (doc_id,),
            ).fetchall()
        return [
            ChunkRow(
                seq=row["seq"],
                length=row["length"],
                text=row["text"],
                entry_quote=row["entry_quote"],
                escape_pending=bool(row["escape_pending"]),
                summary=_summary_from_json(
                    row["closers"], row["openers"], row["mismatches"]
                ),
            )
            for row in rows
        ]

    def replace_chunk_range(
        self,
        doc_id: int,
        first_seq: int,
        last_seq: int,
        new_chunks: list[tuple[str, str | None, bool, Summary]],
        tail_shift: int,
    ) -> None:
        """Replace chunks [first_seq, last_seq] with `new_chunks`.

        Each new chunk is (text, entry_quote, escape_pending, summary).
        `tail_shift` is how much the sequence numbers of all chunks after
        `last_seq` move (new_count - old_count). Only renumbering happens for
        those later chunks — text, summaries and lexer states stay untouched.
        """
        old_count = last_seq - first_seq + 1
        assert tail_shift == len(new_chunks) - old_count
        with self._lock:
            # Move the untouched tail out of the way first to avoid primary
            # key collisions, then into its final position.
            self._conn.execute(
                "UPDATE chunks SET seq = seq + 1000000"
                " WHERE doc_id = ? AND seq > ?",
                (doc_id, last_seq),
            )
            self._conn.execute(
                "DELETE FROM chunks WHERE doc_id = ? AND seq BETWEEN ? AND ?",
                (doc_id, first_seq, last_seq),
            )
            for offset, (text, entry_quote, escape_pending, summary) in enumerate(
                new_chunks
            ):
                closers, openers, mismatches = _summary_to_json(summary)
                self._conn.execute(
                    "INSERT INTO chunks (doc_id, seq, length, text, entry_quote,"
                    " escape_pending, closers, openers, mismatches)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        first_seq + offset,
                        len(text),
                        text,
                        entry_quote,
                        1 if escape_pending else 0,
                        closers,
                        openers,
                        mismatches,
                    ),
                )
            self._conn.execute(
                "UPDATE chunks SET seq = seq - 1000000 + ?"
                " WHERE doc_id = ? AND seq >= 1000000",
                (tail_shift, doc_id),
            )

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()
