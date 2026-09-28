"""Evidence store backed by SQLite.

Responsibilities that genuinely belong here (not in the engine):

* persist *input versions* and the exact tuples that are the evidence,
* record a content hash per relation so a version is immutable and identifiable,
* record each query run together with the answer rows AND their provenance
  polynomial, so an answer and its provenance can only ever be read back from
  the same committed input version,
* validate synthetic input fixtures at the system boundary.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .errors import SnapshotError

SCHEMA = """
CREATE TABLE IF NOT EXISTS relations (
    version      TEXT NOT NULL,
    name         TEXT NOT NULL,
    columns      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (version, name)
);
CREATE TABLE IF NOT EXISTS tuples (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    version  TEXT NOT NULL,
    relation TEXT NOT NULL,
    tuple_id TEXT NOT NULL,
    data     TEXT NOT NULL,
    ordinal  INTEGER NOT NULL,
    UNIQUE (version, relation, tuple_id)
);
CREATE TABLE IF NOT EXISTS query_runs (
    request_id  TEXT PRIMARY KEY,
    versions    TEXT NOT NULL,
    plan_hash   TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS answer_rows (
    request_id   TEXT NOT NULL,
    row_number   INTEGER NOT NULL,
    output_key   TEXT NOT NULL,
    multiplicity INTEGER NOT NULL,
    poly_json    TEXT NOT NULL,
    PRIMARY KEY (request_id, row_number),
    FOREIGN KEY (request_id) REFERENCES query_runs(request_id)
);
"""


@dataclass(frozen=True)
class InputTuple:
    tuple_id: str
    data: dict[str, Any]


@dataclass(frozen=True)
class RelationSnapshot:
    name: str
    columns: tuple[str, ...]
    rows: tuple[InputTuple, ...]


@dataclass(frozen=True)
class Snapshot:
    version: str
    relations: tuple[RelationSnapshot, ...]

    @staticmethod
    def from_dict(raw: Any) -> "Snapshot":
        if not isinstance(raw, dict):
            raise SnapshotError("snapshot root must be an object")
        version = raw.get("version")
        if not isinstance(version, str) or not version:
            raise SnapshotError("snapshot needs a non-empty string 'version'")
        raw_relations = raw.get("relations")
        if not isinstance(raw_relations, dict) or not raw_relations:
            raise SnapshotError("snapshot 'relations' must be a non-empty object")

        relations: list[RelationSnapshot] = []
        for name, body in raw_relations.items():
            if not isinstance(name, str) or not name:
                raise SnapshotError("relation names must be non-empty strings")
            if not isinstance(body, dict):
                raise SnapshotError(f"relation {name!r} must be an object")
            columns = body.get("columns")
            if not isinstance(columns, list) or not columns or not all(
                isinstance(c, str) and c for c in columns
            ):
                raise SnapshotError(f"relation {name!r} needs a non-empty 'columns' list")
            if len(set(columns)) != len(columns):
                raise SnapshotError(f"relation {name!r} has duplicate columns")
            rows_raw = body.get("rows")
            if not isinstance(rows_raw, list):
                raise SnapshotError(f"relation {name!r} 'rows' must be a list")

            rows: list[InputTuple] = []
            seen_ids: set[str] = set()
            for index, row in enumerate(rows_raw):
                if not isinstance(row, dict) or "id" not in row or "data" not in row:
                    raise SnapshotError(f"{name}[{index}] must be {{'id','data'}}")
                tuple_id = str(row["id"])
                if tuple_id in seen_ids:
                    raise SnapshotError(f"{name} has duplicate tuple id {tuple_id!r}")
                seen_ids.add(tuple_id)
                data = row["data"]
                if not isinstance(data, dict):
                    raise SnapshotError(f"{name}[{tuple_id}] 'data' must be an object")
                if set(data) != set(columns):
                    raise SnapshotError(
                        f"{name}[{tuple_id}] data keys {sorted(data)} != columns {sorted(columns)}"
                    )
                for col, value in data.items():
                    if value is not None and not isinstance(value, (str, int, float, bool)):
                        raise SnapshotError(f"{name}[{tuple_id}].{col} must be a scalar or null")
                rows.append(InputTuple(tuple_id=tuple_id, data=dict(data)))
            relations.append(
                RelationSnapshot(name=name, columns=tuple(columns), rows=tuple(rows))
            )
        return Snapshot(version=version, relations=tuple(relations))


def _relation_hash(columns: tuple[str, ...], rows: tuple[InputTuple, ...]) -> str:
    canonical = json.dumps(
        {"columns": list(columns), "rows": [[r.tuple_id, r.data] for r in rows]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EvidenceStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # The service may be reached by FastAPI's worker thread pool; share one
        # connection across threads and serialise access with a lock.
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EvidenceStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- write side -------------------------------------------------------
    def load_snapshot(self, snapshot: Snapshot, *, replace: bool = False) -> dict[str, str]:
        """Insert a versioned snapshot. Returns ``{relation: content_hash}``."""
        with self._lock:
            return self._load_snapshot_locked(snapshot, replace=replace)

    def _load_snapshot_locked(self, snapshot: Snapshot, *, replace: bool) -> dict[str, str]:
        hashes: dict[str, str] = {}
        try:
            for rel in snapshot.relations:
                exists = self._conn.execute(
                    "SELECT 1 FROM relations WHERE version=? AND name=?",
                    (snapshot.version, rel.name),
                ).fetchone()
                if exists and not replace:
                    raise SnapshotError(
                        f"relation {rel.name!r} already exists for version {snapshot.version!r}",
                        details={"version": snapshot.version, "relation": rel.name},
                    )
                content_hash = _relation_hash(rel.columns, rel.rows)
                if exists:
                    self._conn.execute(
                        "DELETE FROM tuples WHERE version=? AND relation=?",
                        (snapshot.version, rel.name),
                    )
                self._conn.execute(
                    "INSERT OR REPLACE INTO relations(version,name,columns,content_hash)"
                    " VALUES(?,?,?,?)",
                    (snapshot.version, rel.name, json.dumps(list(rel.columns)), content_hash),
                )
                for ordinal, row in enumerate(rel.rows):
                    self._conn.execute(
                        "INSERT OR REPLACE INTO tuples(version,relation,tuple_id,data,ordinal)"
                        " VALUES(?,?,?,?,?)",
                        (
                            snapshot.version,
                            rel.name,
                            row.tuple_id,
                            json.dumps(row.data, ensure_ascii=False),
                            ordinal,
                        ),
                    )
                hashes[rel.name] = content_hash
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        return hashes

    # -- read side --------------------------------------------------------
    def require_relation(self, version: str, name: str) -> RelationSnapshot:
        with self._lock:
            return self._require_relation_locked(version, name)

    def _require_relation_locked(self, version: str, name: str) -> RelationSnapshot:
        row = self._conn.execute(
            "SELECT columns FROM relations WHERE version=? AND name=?", (version, name)
        ).fetchone()
        if row is None:
            from .errors import InputVersionError

            raise InputVersionError(
                f"relation {name!r} not found at version {version!r}",
                details={"version": version, "relation": name},
            )
        columns = tuple(json.loads(row["columns"]))
        tuple_rows = self._conn.execute(
            "SELECT tuple_id,data FROM tuples WHERE version=? AND relation=? ORDER BY ordinal",
            (version, name),
        ).fetchall()
        return RelationSnapshot(
            name=name,
            columns=columns,
            rows=tuple(
                InputTuple(tuple_id=r["tuple_id"], data=json.loads(r["data"])) for r in tuple_rows
            ),
        )

    def content_hash(self, version: str, name: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT content_hash FROM relations WHERE version=? AND name=?", (version, name)
            ).fetchone()
        if row is None:
            from .errors import InputVersionError

            raise InputVersionError(
                f"relation {name!r} not found at version {version!r}",
                details={"version": version, "relation": name},
            )
        return row["content_hash"]

    def iter_tuples(self, version: str, name: str) -> Iterator[InputTuple]:
        rel = self.require_relation(version, name)
        yield from rel.rows

    # -- query run / answer persistence ----------------------------------
    def start_run(self, request_id: str, versions: list[str], plan_hash: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO query_runs(request_id,versions,plan_hash,status) VALUES(?,?,?,?)",
                (request_id, json.dumps(sorted(set(versions))), plan_hash, "running"),
            )
            self._conn.commit()

    def finish_run(self, request_id: str, status: str, answers: list[dict[str, Any]]) -> None:
        with self._lock:
            try:
                self._conn.execute(
                    "UPDATE query_runs SET status=? WHERE request_id=?", (status, request_id)
                )
                self._conn.execute("DELETE FROM answer_rows WHERE request_id=?", (request_id,))
                for i, ans in enumerate(answers):
                    self._conn.execute(
                        "INSERT INTO answer_rows(request_id,row_number,output_key,multiplicity,poly_json)"
                        " VALUES(?,?,?,?,?)",
                        (
                            request_id,
                            i,
                            json.dumps(ans["output"], sort_keys=True, ensure_ascii=False),
                            ans["multiplicity"],
                            json.dumps(ans["poly"], ensure_ascii=False),
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def read_back(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            run = self._conn.execute(
                "SELECT * FROM query_runs WHERE request_id=?", (request_id,)
            ).fetchone()
            if run is None:
                return None
            rows = self._conn.execute(
                "SELECT output_key,multiplicity,poly_json FROM answer_rows"
                " WHERE request_id=? ORDER BY row_number",
                (request_id,),
            ).fetchall()
        return {
            "request_id": request_id,
            "versions": json.loads(run["versions"]),
            "status": run["status"],
            "rows": [
                {
                    "output": json.loads(r["output_key"]),
                    "multiplicity": r["multiplicity"],
                    "poly": json.loads(r["poly_json"]),
                }
                for r in rows
            ],
        }
