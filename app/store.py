"""SQLite persistence for corpora and their serialized indexes."""

from __future__ import annotations

import json
import sqlite3
import uuid
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from app.config import INDEX_FORMAT_VERSION
from app.corpus import Document, SymbolStream
from app.errors import AppError, FailureCategory
from app.index import Index

_SCHEMA = """
CREATE TABLE IF NOT EXISTS corpora (
    corpus_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    doc_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    index_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
    corpus_id TEXT NOT NULL,
    doc_index INTEGER NOT NULL,
    doc_id TEXT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY (corpus_id, doc_index)
);
CREATE TABLE IF NOT EXISTS indexes (
    corpus_id TEXT PRIMARY KEY,
    symbols BLOB NOT NULL,
    sa BLOB NOT NULL,
    lcp BLOB NOT NULL,
    doc_of BLOB NOT NULL,
    extent BLOB NOT NULL,
    doc_starts BLOB NOT NULL,
    doc_ids TEXT NOT NULL
);
"""


def _pack(values: list[int]) -> bytes:
    return array("q", values).tobytes()


def _unpack(blob: bytes) -> list[int]:
    data = array("q")
    data.frombytes(blob)
    return list(data)


class CorpusStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save_corpus(self, name: str, docs: list[Document], index: Index) -> dict:
        corpus_id = uuid.uuid4().hex[:12]
        created_at = datetime.now(timezone.utc).isoformat()
        total_bytes = sum(len(d.content) for d in docs)
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO corpora VALUES (?, ?, ?, ?, ?, ?)",
                (corpus_id, name, created_at, len(docs), total_bytes, INDEX_FORMAT_VERSION),
            )
            conn.executemany(
                "INSERT INTO documents VALUES (?, ?, ?, ?)",
                [
                    (corpus_id, i, d.doc_id, sqlite3.Binary(d.content))
                    for i, d in enumerate(docs)
                ],
            )
            conn.execute(
                "INSERT INTO indexes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    corpus_id,
                    _pack(index.stream.symbols),
                    _pack(index.sa),
                    _pack(index.lcp),
                    _pack(index.stream.doc_of),
                    _pack(index.stream.extent),
                    _pack(index.stream.doc_starts),
                    json.dumps(index.doc_ids),
                ),
            )
        return self.get_corpus_meta(corpus_id)

    def get_corpus_meta(self, corpus_id: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM corpora WHERE corpus_id = ?", (corpus_id,)
            ).fetchone()
        if row is None:
            raise AppError(
                FailureCategory.CORPUS_NOT_FOUND, f"unknown corpus {corpus_id!r}"
            )
        return dict(row)

    def load_index(self, corpus_id: str) -> Index:
        meta = self.get_corpus_meta(corpus_id)
        if meta["index_version"] != INDEX_FORMAT_VERSION:
            raise AppError(
                FailureCategory.INDEX_CORRUPT,
                f"corpus {corpus_id!r} was indexed with format version "
                f"{meta['index_version']}, expected {INDEX_FORMAT_VERSION}",
            )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM indexes WHERE corpus_id = ?", (corpus_id,)
            ).fetchone()
        if row is None:
            raise AppError(
                FailureCategory.INDEX_CORRUPT,
                f"corpus {corpus_id!r} has no stored index",
            )
        stream = SymbolStream(
            symbols=_unpack(row["symbols"]),
            doc_of=_unpack(row["doc_of"]),
            extent=_unpack(row["extent"]),
            doc_starts=_unpack(row["doc_starts"]),
        )
        return Index(
            stream=stream,
            sa=_unpack(row["sa"]),
            lcp=_unpack(row["lcp"]),
            doc_ids=list(json.loads(row["doc_ids"])),
        )
