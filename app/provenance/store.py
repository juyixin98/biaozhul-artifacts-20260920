"""Versioned evidence store backed by SQLite.

Every input row lives under an immutable *version*.  Answers and provenance are
always requested against an explicit version id, so a query can never mix rows
from two different input versions.

Relation and column names are stored as **data** (columns in the EAV tables
below), never interpolated into SQL, so user-supplied identifiers cannot alter
the issued SQL.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


class VersionNotFound(LookupError):
    """Raised when a caller references a version id that does not exist."""


@dataclass(frozen=True)
class InputRow:
    """One witnessed input tuple."""

    row_id: str
    values: tuple
    weight: float = 1.0


@dataclass(frozen=True)
class RelationData:
    name: str
    columns: tuple[str, ...]
    rows: tuple[InputRow, ...]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    label      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS relations (
    version_id INTEGER NOT NULL REFERENCES versions(version_id),
    name       TEXT NOT NULL,
    columns    TEXT NOT NULL,
    PRIMARY KEY (version_id, name)
);
CREATE TABLE IF NOT EXISTS rows (
    version_id INTEGER NOT NULL,
    relation   TEXT NOT NULL,
    row_id     TEXT NOT NULL,
    ordinal    INTEGER NOT NULL,
    payload    TEXT NOT NULL,
    weight     REAL NOT NULL,
    PRIMARY KEY (version_id, relation, row_id)
);
CREATE INDEX IF NOT EXISTS idx_rows_version_relation
    ON rows (version_id, relation);
"""


class EvidenceStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    # ---- write path ----------------------------------------------------

    def create_version(self, label: str) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO versions (label, created_at) VALUES (?, ?)",
                (label, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def add_relation(
        self,
        version_id: int,
        name: str,
        columns: Sequence[str],
        rows: Sequence[InputRow],
    ) -> None:
        self._require_version(version_id)
        if not columns:
            raise ValueError(f"relation {name!r} must declare at least one column")
        for row in rows:
            if len(row.values) != len(columns):
                raise ValueError(
                    f"row {row.row_id!r} in {name!r} has {len(row.values)} values "
                    f"but {len(columns)} columns declared"
                )
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO relations (version_id, name, columns) VALUES (?, ?, ?)",
                (version_id, name, json.dumps(list(columns))),
            )
            conn.executemany(
                "INSERT INTO rows (version_id, relation, row_id, ordinal, "
                "payload, weight) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        version_id,
                        name,
                        row.row_id,
                        ordinal,
                        json.dumps(list(row.values)),
                        float(row.weight),
                    )
                    for ordinal, row in enumerate(rows)
                ],
            )
            conn.commit()

    # ---- read path -----------------------------------------------------

    def latest_version(self) -> int | None:
        with self._connect() as conn:
            cur = conn.execute("SELECT MAX(version_id) FROM versions")
            (value,) = cur.fetchone()
            return None if value is None else int(value)

    def _require_version(self, version_id: int) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM versions WHERE version_id = ?", (version_id,)
            )
            if cur.fetchone() is None:
                raise VersionNotFound(f"input version {version_id} does not exist")

    def list_relations(self, version_id: int) -> list[str]:
        self._require_version(version_id)
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT name FROM relations WHERE version_id = ? ORDER BY name",
                (version_id,),
            )
            return [name for (name,) in cur.fetchall()]

    def fetch_schema(self, version_id: int) -> dict[str, tuple[str, ...]]:
        self._require_version(version_id)
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT name, columns FROM relations WHERE version_id = ?",
                (version_id,),
            )
            return {name: tuple(json.loads(cols)) for name, cols in cur.fetchall()}

    def fetch_relation(self, version_id: int, name: str) -> RelationData:
        self._require_version(version_id)
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT columns FROM relations WHERE version_id = ? AND name = ?",
                (version_id, name),
            )
            row = cur.fetchone()
            if row is None:
                raise KeyError(
                    f"relation {name!r} does not exist in input version {version_id}"
                )
            columns = tuple(json.loads(row[0]))
            cur = conn.execute(
                "SELECT row_id, payload, weight FROM rows "
                "WHERE version_id = ? AND relation = ? ORDER BY ordinal",
                (version_id, name),
            )
            rows = tuple(
                InputRow(
                    row_id=row_id,
                    values=tuple(json.loads(payload)),
                    weight=weight,
                )
                for row_id, payload, weight in cur.fetchall()
            )
        return RelationData(name=name, columns=columns, rows=rows)

    def fetch_weights(self, version_id: int) -> dict[str, float]:
        """Map every witness id ``relation.row_id`` to its numeric weight."""
        self._require_version(version_id)
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT relation, row_id, weight FROM rows WHERE version_id = ?",
                (version_id,),
            )
            return {f"{relation}.{row_id}": weight for relation, row_id, weight in cur}
