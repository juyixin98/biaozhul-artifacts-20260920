"""SQLite persistence: documents, index arrays, metadata, query audit log.

The database is the single source of truth across restarts: the service
rebuilds the in-memory kernel from the persisted suffix/LCP arrays instead of
recomputing them. Integrity is checked at load time (array lengths, document
count, kernel version); any mismatch raises ``INDEX_CORRUPT`` or
``KERNEL_VERSION_MISMATCH`` rather than serving stale results.
"""

from __future__ import annotations

import json
import sqlite3
import struct
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import KERNEL_VERSION
from .corpus import Document, EncodedCorpus, encode_corpus
from .validation import ErrorCategory, LcsError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    seq INTEGER PRIMARY KEY,
    doc_id TEXT NOT NULL UNIQUE,
    content BLOB NOT NULL,
    sha256 TEXT NOT NULL,
    length INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS index_arrays (
    name TEXT PRIMARY KEY,
    data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS index_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS query_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    request_id TEXT NOT NULL,
    query_id TEXT,
    params_json TEXT NOT NULL,
    status TEXT NOT NULL,
    summary_json TEXT,
    error_json TEXT
);
"""

_UINT32 = struct.Struct("<I")


def _pack_uint32(values: list[int] | tuple[int, ...]) -> bytes:
    return b"".join(_UINT32_PACKER(v) for v in values)


_UINT32_PACKER = _UINT32.pack


def _unpack_uint32(data: bytes) -> list[int]:
    if len(data) % _UINT32.size != 0:
        raise LcsError(ErrorCategory.INDEX_CORRUPT, "array blob has odd byte length")
    count = len(data) // _UINT32.size
    return list(struct.unpack(f"<{count}I", data))


@dataclass(frozen=True)
class StoredIndex:
    corpus: EncodedCorpus
    suffix_array: list[int]
    lcp_array: list[int]
    meta: dict


class IndexStore:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- index lifecycle ----------------------------------------------------

    def save_index(
        self,
        corpus: EncodedCorpus,
        suffix_array: list[int],
        lcp_array: list[int],
        corpus_sha256: str,
        doc_sha256: dict[str, str],
    ) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("DELETE FROM index_arrays")
            self._conn.execute("DELETE FROM index_meta")
            for seq, doc in enumerate(corpus.documents):
                self._conn.execute(
                    "INSERT INTO documents(seq, doc_id, content, sha256, length)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (seq, doc.doc_id, doc.content, doc_sha256[doc.doc_id], len(doc.content)),
                )
            self._conn.execute(
                "INSERT INTO index_arrays(name, data) VALUES ('sa', ?)",
                (_pack_uint32(suffix_array),),
            )
            self._conn.execute(
                "INSERT INTO index_arrays(name, data) VALUES ('lcp', ?)",
                (_pack_uint32(lcp_array),),
            )
            meta = {
                "kernel_version": KERNEL_VERSION,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "doc_count": str(corpus.doc_count),
                "symbol_count": str(len(corpus.symbols)),
                "corpus_sha256": corpus_sha256,
            }
            self._conn.executemany(
                "INSERT INTO index_meta(key, value) VALUES (?, ?)", meta.items()
            )

    def has_index(self) -> bool:
        row = self._conn.execute(
            "SELECT value FROM index_meta WHERE key = 'kernel_version'"
        ).fetchone()
        return row is not None

    def load_index(self) -> StoredIndex:
        if not self.has_index():
            raise LcsError(ErrorCategory.INDEX_NOT_FOUND, "no index has been built")

        meta = {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM index_meta")
        }
        if meta["kernel_version"] != KERNEL_VERSION:
            raise LcsError(
                ErrorCategory.KERNEL_VERSION_MISMATCH,
                f"index was built with {meta['kernel_version']}, "
                f"service runs {KERNEL_VERSION}; rebuild the index",
            )

        doc_rows = self._conn.execute(
            "SELECT doc_id, content FROM documents ORDER BY seq"
        ).fetchall()
        documents = [Document(doc_id=row["doc_id"], content=row["content"]) for row in doc_rows]
        corpus = encode_corpus(documents)

        arrays = {
            row["name"]: row["data"]
            for row in self._conn.execute("SELECT name, data FROM index_arrays")
        }
        sa = _unpack_uint32(arrays["sa"])
        lcp = _unpack_uint32(arrays["lcp"])

        expected_symbols = len(corpus.symbols)
        if len(sa) != expected_symbols or len(lcp) != expected_symbols:
            raise LcsError(
                ErrorCategory.INDEX_CORRUPT,
                f"array length mismatch: sa={len(sa)} lcp={len(lcp)} "
                f"symbols={expected_symbols}",
            )
        if int(meta["doc_count"]) != len(documents):
            raise LcsError(ErrorCategory.INDEX_CORRUPT, "document count mismatch")
        if int(meta["symbol_count"]) != expected_symbols:
            raise LcsError(ErrorCategory.INDEX_CORRUPT, "symbol count mismatch")

        return StoredIndex(corpus=corpus, suffix_array=sa, lcp_array=lcp, meta=meta)

    def index_info(self) -> dict | None:
        if not self.has_index():
            return None
        meta = {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM index_meta")
        }
        docs = self._conn.execute(
            "SELECT doc_id, length, sha256 FROM documents ORDER BY seq"
        ).fetchall()
        return {
            "kernel_version": meta["kernel_version"],
            "created_at": meta["created_at"],
            "doc_count": int(meta["doc_count"]),
            "symbol_count": int(meta["symbol_count"]),
            "corpus_sha256": meta["corpus_sha256"],
            "documents": [dict(row) for row in docs],
        }

    # -- audit log ------------------------------------------------------------

    def log_query(
        self,
        request_id: str,
        query_id: str | None,
        params: dict,
        status: str,
        summary: dict | None,
        error: dict | None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO query_log(ts, request_id, query_id, params_json,"
                " status, summary_json, error_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(timezone.utc).isoformat(),
                    request_id,
                    query_id,
                    json.dumps(params, sort_keys=True),
                    status,
                    json.dumps(summary, sort_keys=True) if summary is not None else None,
                    json.dumps(error, sort_keys=True) if error is not None else None,
                ),
            )

    def recent_queries(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT ts, request_id, query_id, params_json, status, summary_json,"
            " error_json FROM query_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
