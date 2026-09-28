"""SQLite persistence.

Two tables:

* ``documents``     -- metadata, current offset version and length;
* ``bracket_blocks``-- one row per rope leaf: the raw chunk text plus its
  treap routing data, subtree length and encoded :class:`Reduction`.

Edits only touch rows for the extracted local span (plus ancestor pulls held
in memory and upserted as dirty); unrelated block rows are never re-read or
re-written. All queries are parameterized.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .mining.tokens import Reduction

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    lexicon_name TEXT NOT NULL,
    length INTEGER NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS bracket_blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    left_id INTEGER,
    right_id INTEGER,
    text TEXT NOT NULL,
    sub_len INTEGER NOT NULL,
    red_json TEXT NOT NULL,
    FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_blocks_doc ON bracket_blocks(document_id);
"""


# -- Reduction JSON codec ---------------------------------------------------

def _tok(t) -> list:
    return [t.offset, t.kind, t.type, t.char]


def _pair(p) -> list:
    return [_tok(p[0]), _tok(p[1])]


def encode_reduction(r: Reduction) -> str:
    return json.dumps(
        {
            "prefix": [_tok(t) for t in r.prefix],
            "suffix": [_tok(t) for t in r.suffix],
            "matches": [_pair(p) for p in r.matches],
            "mismatches": [_pair(p) for p in r.mismatches],
        },
        separators=(",", ":"),
    )


def _untok(row: list):
    from .mining.lexer import Token

    return Token(offset=row[0], kind=row[1], type=row[2], char=row[3])


def decode_reduction(blob: str) -> Reduction:
    data = json.loads(blob)
    return Reduction(
        prefix=tuple(_untok(t) for t in data["prefix"]),
        suffix=tuple(_untok(t) for t in data["suffix"]),
        matches=tuple((_untok(p[0]), _untok(p[1])) for p in data["matches"]),
        mismatches=tuple(
            (_untok(p[0]), _untok(p[1])) for p in data["mismatches"]
        ),
    )


@dataclass(frozen=True)
class DocumentRecord:
    id: int
    name: str
    lexicon_name: str
    length: int
    version: int
    created_at: str


class Database:
    """Thin sqlite wrapper; the repository owns SQL, callers own transactions."""

    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._apply_schema()

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    def _apply_schema(self) -> None:
        with self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()


class DocumentRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def create_document(self, name: str, lexicon_name: str) -> int:
        cur = self._db.conn.execute(
            "INSERT INTO documents(name, lexicon_name, length, version) "
            "VALUES(?, ?, 0, 0)",
            (name, lexicon_name),
        )
        return int(cur.lastrowid)

    def get(self, doc_id: int) -> DocumentRecord | None:
        row = self._db.conn.execute(
            "SELECT id, name, lexicon_name, length, version, created_at "
            "FROM documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        return self._to_record(row) if row else None

    def find_by_name(self, name: str) -> DocumentRecord | None:
        row = self._db.conn.execute(
            "SELECT id, name, lexicon_name, length, version, created_at "
            "FROM documents WHERE name = ?",
            (name,),
        ).fetchone()
        return self._to_record(row) if row else None

    def list_documents(self) -> list[DocumentRecord]:
        rows = self._db.conn.execute(
            "SELECT id, name, lexicon_name, length, version, created_at "
            "FROM documents ORDER BY id"
        ).fetchall()
        return [self._to_record(r) for r in rows]

    def update_length_version(self, doc_id: int, length: int, version: int) -> None:
        self._db.conn.execute(
            "UPDATE documents SET length = ?, version = ? WHERE id = ?",
            (length, version, doc_id),
        )

    def delete(self, doc_id: int) -> None:
        self._db.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))

    @staticmethod
    def _to_record(row: sqlite3.Row) -> DocumentRecord:
        return DocumentRecord(
            id=row["id"],
            name=row["name"],
            lexicon_name=row["lexicon_name"],
            length=row["length"],
            version=row["version"],
            created_at=row["created_at"],
        )
