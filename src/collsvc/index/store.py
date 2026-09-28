"""Version-gated SQLite index store.

Invariants enforced here:

* both the **original text** (``text``) and the **sort key** (``sort_key``)
  are persisted, plus the NFC form and the primary section;
* an index row carries the exact :class:`IndexVersion` it was built under in
  ``index_meta`` — strength / numeric / case-first cannot change silently;
* opening a store checks the requested options against the stored version and
  raises :class:`VersionMismatchError` instead of mixing keys;
* rebuilding replaces rows transactionally and bumps the version, which
  invalidates every previously issued cursor (checked again at query time).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from ..collation.engine import CollationEngine
from ..collation.options import CollationOptions, STRENGTH_IDENTICAL
from ..collation.versioning import IndexVersion, VersionMismatchError
from ..corpus.spec import CorpusSpec
from ..mining.miner import MinedRecord, mine
from ..query.keys import primary_section, primary_upper_bound
from .schema import SCHEMA_SQL


class IndexNotFoundError(RuntimeError):
    """No index has been built in this database yet."""


class IndexStore:
    def __init__(self, db_path: Path, options: CollationOptions) -> None:
        options.validate()
        self._db_path = Path(db_path)
        self._options = options
        self._engine = CollationEngine(options)
        self._expected_version = IndexVersion.for_engine(self._engine)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._meta: dict[str, str] | None = self._load_meta()

    # ---------- lifecycle ----------

    @property
    def engine(self) -> CollationEngine:
        return self._engine

    @property
    def options(self) -> CollationOptions:
        return self._options

    @property
    def expected_version(self) -> IndexVersion:
        return self._expected_version

    @property
    def is_built(self) -> bool:
        return self._meta is not None

    @property
    def current_version(self) -> str | None:
        return self._meta.get("index_version") if self._meta else None

    def _ensure_schema(self) -> None:
        self._conn.executescript(SCHEMA_SQL)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "IndexStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---------- version gating ----------

    def require_open(self) -> dict[str, str]:
        """Return stored metadata, refusing to use an absent/mismatched index."""
        if self._meta is None:
            raise IndexNotFoundError(
                f"no index exists in {self._db_path}; build/rebuild first"
            )
        stored = self._meta["index_version"]
        if stored != self._expected_version.as_token():
            raise VersionMismatchError(
                f"index was built under {stored} but current options resolve to "
                f"{self._expected_version.as_token()}; rebuild required "
                "(old cursors will be rejected)"
            )
        return self._meta

    # ---------- build / rebuild ----------

    def build(self, spec: CorpusSpec, *, replace: bool = False) -> dict[str, Any]:
        if self._meta is not None and not replace:
            stored = self._meta["index_version"]
            if stored == self._expected_version.as_token():
                return {"status": "unchanged", "index_version": stored}
            raise VersionMismatchError(
                f"refusing to overwrite index {stored} without replace=True"
            )

        report = mine(spec)
        corpus_hash = hashlib.sha256(
            "\n".join(f"{r.doc_id}\0{r.text}" for r in report.records).encode("utf-8")
        ).hexdigest()

        self._conn.execute("BEGIN")
        try:
            self._conn.execute("DELETE FROM entries")
            self._conn.execute("DELETE FROM index_meta")
            self._conn.execute("DELETE FROM build_events")
            self._insert_records(report.records)
            meta = self._build_meta(spec.name, corpus_hash, len(report.records))
            self._write_meta(meta)
            self._conn.execute(
                "INSERT INTO build_events(event, detail) VALUES (?, ?)",
                (
                    "rebuild" if replace else "build",
                    json.dumps(
                        {
                            "index_version": meta["index_version"],
                            "row_count": meta["row_count"],
                            "locale": self._options.locale,
                            "strength": self._options.strength,
                            "numeric": self._options.numeric,
                            "case_first": self._options.case_first,
                        }
                    ),
                ),
            )
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        self._meta = meta
        return {
            "status": "rebuilt" if replace else "built",
            "index_version": meta["index_version"],
            "row_count": int(meta["row_count"]),
            "mining": report.stats,
        }

    def _insert_records(self, records: Iterable[MinedRecord]) -> None:
        rows = []
        for seq, rec in enumerate(records):
            key = self._engine.sort_key(rec.text)
            rows.append(
                (
                    rec.doc_id,
                    rec.text,
                    rec.nfc,
                    key,
                    primary_section(key),
                    seq,
                )
            )
        self._conn.executemany(
            "INSERT INTO entries(doc_id, text, nfc_text, sort_key, primary_key, seq)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    def _build_meta(self, corpus_name: str, corpus_hash: str, row_count: int) -> dict[str, str]:
        runtime = self._engine.runtime
        return {
            "index_version": self._expected_version.as_token(),
            "schema_generation": str(self._expected_version.schema_generation),
            "rules_fingerprint": self._expected_version.rules_fingerprint,
            "options_json": json.dumps(
                self._options.canonical_dict(), sort_keys=True
            ),
            "icu_version": runtime.icu_version,
            "unicode_version": runtime.unicode_version,
            "actual_locale": self._engine.actual_locale,
            "corpus_name": corpus_name,
            "corpus_sha256": corpus_hash,
            "row_count": str(row_count),
            "built_at": datetime.now(timezone.utc).isoformat(),
        }

    def _write_meta(self, meta: dict[str, str]) -> None:
        self._conn.executemany(
            "INSERT INTO index_meta(meta_key, meta_value) VALUES (?, ?)",
            meta.items(),
        )

    def _load_meta(self) -> dict[str, str] | None:
        rows = self._conn.execute("SELECT meta_key, meta_value FROM index_meta").fetchall()
        if not rows:
            return None
        return {r["meta_key"]: r["meta_value"] for r in rows}

    # ---------- read primitives (all key-ordered, never UTF-8 ordered) ----------

    def key_for(self, text: str) -> bytes:
        return self._engine.sort_key(text)

    def list_page(
        self, after_key: bytes | None, after_seq: int, limit: int
    ) -> list[sqlite3.Row]:
        """Keyset pagination in full sort-key order, ``seq`` as stable tie-break."""
        if after_key is None:
            sql = (
                "SELECT * FROM entries ORDER BY sort_key, seq LIMIT ?"
            )
            return list(self._conn.execute(sql, (limit,)).fetchall())
        sql = (
            "SELECT * FROM entries"
            " WHERE (sort_key > ?) OR (sort_key = ? AND seq > ?)"
            " ORDER BY sort_key, seq LIMIT ?"
        )
        return list(
            self._conn.execute(sql, (after_key, after_key, after_seq, limit)).fetchall()
        )

    def range_page(
        self,
        low_key: bytes,
        high_key: bytes,
        after_key: bytes | None,
        after_seq: int,
        limit: int,
    ) -> list[sqlite3.Row]:
        """Inclusive value range, boundaries compared as ICU sort-key BLOBs."""
        if after_key is None:
            sql = (
                "SELECT * FROM entries"
                " WHERE sort_key >= ? AND sort_key <= ?"
                " ORDER BY sort_key, seq LIMIT ?"
            )
            return list(self._conn.execute(sql, (low_key, high_key, limit)).fetchall())
        sql = (
            "SELECT * FROM entries"
            " WHERE sort_key >= ? AND sort_key <= ?"
            " AND ((sort_key > ?) OR (sort_key = ? AND seq > ?))"
            " ORDER BY sort_key, seq LIMIT ?"
        )
        return list(
            self._conn.execute(
                sql, (low_key, high_key, after_key, after_key, after_seq, limit)
            ).fetchall()
        )

    def primary_seek_candidates(
        self, prefix_key: bytes, limit: int, offset: int = 0
    ) -> list[sqlite3.Row]:
        """Indexed primary-section superset used to narrow prefix queries.

        Bounds are compared against the stored **primary** column: lower is
        the prefix's primary section and upper is that section plus headroom.
        """
        lower = primary_section(prefix_key)
        upper = primary_upper_bound(prefix_key)
        sql = (
            "SELECT * FROM entries"
            " WHERE primary_key >= ? AND primary_key < ?"
            " ORDER BY sort_key, seq LIMIT ? OFFSET ?"
        )
        return list(
            self._conn.execute(sql, (lower, upper, limit, offset)).fetchall()
        )

    def all_rows_ordered(self) -> list[sqlite3.Row]:
        """Full ordered scan — only used by explicitly-degraded prefix queries."""
        return list(
            self._conn.execute(
                "SELECT * FROM entries ORDER BY sort_key, seq"
            ).fetchall()
        )

    def total_count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0])

    def count_range(self, low_key: bytes, high_key: bytes) -> int:
        return int(
            self._conn.execute(
                "SELECT COUNT(*) FROM entries WHERE sort_key >= ? AND sort_key <= ?",
                (low_key, high_key),
            ).fetchone()[0]
        )


def seek_supported(engine_or_options) -> bool:
    """Whether the primary-key seek alone guarantees prefix recall.

    Unsafe cases (verified by property tests) require a full scan:

    * ``numeric=True`` — a digit run is merged into one collation weight;
    * ``IDENTICAL`` strength — combining-mark/NFC boundary weights expand
      past the simple primary upper bound;
    * locale contractions (Danish ``aa``, Czech ``ch`` …) — primary weights
      do not concatenate, detected directly by the engine.

    Accepts either a :class:`CollationEngine` (preferred) or plain options.
    """
    # Local import to keep the store module import-light in unit contexts.
    from ..collation.engine import CollationEngine

    if isinstance(engine_or_options, CollationEngine):
        return engine_or_options.primary_prefix_seek_safe()

    options = engine_or_options
    if options.numeric or options.strength == STRENGTH_IDENTICAL:
        return False
    # Without a live engine we cannot rule contractions in or out, so be
    # conservative and force the scan path.
    return CollationEngine(options).primary_prefix_seek_safe()
