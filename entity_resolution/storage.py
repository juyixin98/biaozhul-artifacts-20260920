"""SQLite persistence.

The storage layer owns no entity-resolution logic; it stores records,
constraints, cluster memberships, locks and an audit trail. It returns plain
dicts; the service layer maps them to the pydantic contracts. All writes occur
inside short transactions and are guarded by a process-local lock so the
FastAPI threadpool cannot interleave them.

Schema
------
records         - corpus rows (name, language, attributes JSON, version)
links           - must/cannot constraints (one kind per pair, enforced)
clusters        - cluster identity, lock flag, optimistic version
memberships     - record -> cluster
aliases         - alias spelling -> canonical surface
audit_events    - append-only change log keyed by run_id
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .errors import (
    ConstraintConflictError,
    DuplicateRecordIdError,
    LockViolationError,
    RecordNotFoundError,
    VersionConflictError,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    language   TEXT NOT NULL DEFAULT '',
    attributes TEXT NOT NULL DEFAULT '{}',
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    left_id    TEXT NOT NULL,
    right_id   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('must', 'cannot')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (left_id, right_id)
);
CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    locked     INTEGER NOT NULL DEFAULT 0,
    version    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    cluster_id TEXT NOT NULL REFERENCES clusters(cluster_id),
    record_id  TEXT NOT NULL REFERENCES records(id),
    PRIMARY KEY (record_id)
);
CREATE INDEX IF NOT EXISTS idx_memberships_cluster ON memberships(cluster_id);
CREATE TABLE IF NOT EXISTS aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    operation  TEXT NOT NULL,
    record_id  TEXT,
    before     TEXT,
    after      TEXT,
    at         TEXT NOT NULL
);
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ corpus

    def replace_corpus(
        self,
        records: Iterable[dict],
        must: Iterable[tuple[str, str]],
        cannot: Iterable[tuple[str, str]],
        aliases: dict[str, list[str]],
    ) -> int:
        """Wipe and atomically reload a corpus plus its constraints/aliases."""
        with self._lock, self._conn:
            self._conn.executescript(
                "DELETE FROM memberships; DELETE FROM clusters; "
                "DELETE FROM links; DELETE FROM records; DELETE FROM aliases;"
            )
            now = _utcnow()
            self._conn.executemany(
                "INSERT INTO records(id, name, language, attributes, version, created_at)"
                " VALUES (?,?,?,?,1,?)",
                [
                    (
                        r["id"],
                        r["name"],
                        r.get("language", ""),
                        json.dumps(r.get("attributes", {}), ensure_ascii=False),
                        now,
                    )
                    for r in records
                ],
            )
            self._insert_links(must, "must")
            self._insert_links(cannot, "cannot")
            for canonical, alt_names in aliases.items():
                self._upsert_alias_group(canonical, alt_names)
            return len(list(records)) if not isinstance(records, list) else len(records)

    def insert_record(self, record: dict) -> None:
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO records(id, name, language, attributes, version, created_at)"
                    " VALUES (?,?,?,?,1,?)",
                    (
                        record["id"],
                        record["name"],
                        record.get("language", ""),
                        json.dumps(record.get("attributes", {}), ensure_ascii=False),
                        _utcnow(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateRecordIdError(
                    "record id already exists", {"id": record["id"]}
                ) from exc

    def _insert_links(self, pairs: Iterable[tuple[str, str]], kind: str) -> None:
        now = _utcnow()
        rows = []
        for left, right in pairs:
            a, b = sorted((left, right))
            rows.append((a, b, kind, now))
        self._conn.executemany(
            "INSERT OR IGNORE INTO links(left_id, right_id, kind, created_at)"
            " VALUES (?,?,?,?)",
            rows,
        )

    def _upsert_alias_group(self, canonical: str, alt_names: list[str]) -> None:
        now_names = [canonical, *alt_names]
        self._conn.executemany(
            "INSERT INTO aliases(alias, canonical) VALUES (?,?) "
            "ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical",
            [(name, canonical) for name in now_names],
        )

    # ------------------------------------------------------------------ reads

    def list_records(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records ORDER BY id"
            ).fetchall()
            return [self._record_dict(r) for r in rows]

    def count_records(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def get_record(self, record_id: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM records WHERE id = ?", (record_id,)
            ).fetchone()
            if row is None:
                raise RecordNotFoundError(
                    "record not found", {"id": record_id}
                )
            return self._record_dict(row)

    def _record_dict(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"],
            "name": row["name"],
            "language": row["language"],
            "attributes": json.loads(row["attributes"]),
            "version": row["version"],
            "created_at": row["created_at"],
            "cluster_id": None,
            "locked": False,
        }

    def list_aliases(self) -> dict[str, list[str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT alias, canonical FROM aliases ORDER BY canonical, alias"
            ).fetchall()
        groups: dict[str, list[str]] = {}
        for row in rows:
            groups.setdefault(row["canonical"], []).append(row["alias"])
        return groups

    # ------------------------------------------------------------------ links

    def list_links(self) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT left_id, right_id, kind FROM links"
            ).fetchall()
        must: set[tuple[str, str]] = set()
        cannot: set[tuple[str, str]] = set()
        for row in rows:
            pair = (row["left_id"], row["right_id"])
            (must if row["kind"] == "must" else cannot).add(pair)
        return must, cannot

    def add_link(self, left: str, right: str, kind: str) -> bool:
        """Insert a constraint. Returns False if it already existed.

        Rejects a pair that already carries the opposite kind (state conflict).
        Endpoint existence is validated by the service against loaded records.
        """
        a, b = sorted((left, right))
        opposite = "cannot" if kind == "must" else "must"
        with self._lock, self._conn:
            clash = self._conn.execute(
                "SELECT 1 FROM links WHERE left_id=? AND right_id=? AND kind=?",
                (a, b, opposite),
            ).fetchone()
            if clash:
                raise ConstraintConflictError(
                    "pair already carries the opposite constraint",
                    {"pair": (a, b), "existing": opposite, "reason": "direct_contradiction"},
                )
            existing = self._conn.execute(
                "SELECT 1 FROM links WHERE left_id=? AND right_id=? AND kind=?",
                (a, b, kind),
            ).fetchone()
            if existing:
                return False
            self._conn.execute(
                "INSERT INTO links(left_id, right_id, kind, created_at) VALUES (?,?,?,?)",
                (a, b, kind, _utcnow()),
            )
            return True

    def delete_link(self, left: str, right: str, kind: str) -> bool:
        a, b = sorted((left, right))
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM links WHERE left_id=? AND right_id=? AND kind=?",
                (a, b, kind),
            )
            return cur.rowcount > 0

    # ------------------------------------------------------------------ clusters

    def assignment(self) -> dict[str, str | None]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_id, cluster_id FROM memberships"
            ).fetchall()
        return {row["record_id"]: row["cluster_id"] for row in rows}

    def locked_blocks(self) -> list[frozenset[str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT c.cluster_id, m.record_id FROM clusters c "
                "JOIN memberships m ON m.cluster_id = c.cluster_id "
                "WHERE c.locked = 1 ORDER BY c.cluster_id, m.record_id"
            ).fetchall()
        groups: dict[str, set[str]] = {}
        for row in rows:
            groups.setdefault(row["cluster_id"], set()).add(row["record_id"])
        return [frozenset(parts) for parts in groups.values()]

    def list_clusters(self) -> list[dict]:
        with self._lock:
            cluster_rows = self._conn.execute(
                "SELECT * FROM clusters ORDER BY cluster_id"
            ).fetchall()
            member_rows = self._conn.execute(
                "SELECT cluster_id, record_id FROM memberships ORDER BY record_id"
            ).fetchall()
        grouped: dict[str, list[str]] = {}
        for r in member_rows:
            grouped.setdefault(r["cluster_id"], []).append(r["record_id"])
        out = []
        for row in cluster_rows:
            out.append(
                {
                    "cluster_id": row["cluster_id"],
                    "locked": bool(row["locked"]),
                    "version": row["version"],
                    "members": grouped.get(row["cluster_id"], []),
                }
            )
        return out

    def get_cluster(self, cluster_id: str) -> dict | None:
        for cluster in self.list_clusters():
            if cluster["cluster_id"] == cluster_id:
                return cluster
        return None

    def lock_cluster(
        self,
        cluster_id: str,
        members: list[str],
        expected_version: int | None,
        run_id: str | None = None,
    ) -> dict:
        """Create or seal a cluster mapping as locked.

        Members must not already be locked into a *different* cluster.
        Records the mapping change in the audit log (operation ``lock``).
        """
        members = sorted(set(members))
        run_id = run_id or f"lock-{_utcnow()}"
        with self._lock, self._conn:
            prior = {
                r["record_id"]: r["cluster_id"]
                for r in self._conn.execute(
                    "SELECT record_id, cluster_id FROM memberships"
                ).fetchall()
            }
            row = self._conn.execute(
                "SELECT version FROM clusters WHERE cluster_id=?", (cluster_id,)
            ).fetchone()
            if row is not None:
                if expected_version is not None and row["version"] != expected_version:
                    raise VersionConflictError(
                        "cluster version mismatch",
                        {
                            "cluster_id": cluster_id,
                            "expected": expected_version,
                            "actual": row["version"],
                        },
                    )
                locked = self._conn.execute(
                    "SELECT locked FROM clusters WHERE cluster_id=?", (cluster_id,)
                ).fetchone()["locked"]
                if locked:
                    raise LockViolationError(
                        "cluster is already locked",
                        {"cluster_id": cluster_id, "version": row["version"]},
                    )

            conflicts = self._conn.execute(
                "SELECT m.record_id, m.cluster_id FROM memberships m "
                "JOIN clusters c ON c.cluster_id = m.cluster_id "
                "WHERE c.locked = 1 AND m.record_id IN ({}) AND m.cluster_id != ?".format(
                    ",".join("?" for _ in members)
                ),
                (*members, cluster_id),
            ).fetchall() if members else []
            if conflicts:
                blocked = {r["record_id"]: r["cluster_id"] for r in conflicts}
                raise LockViolationError(
                    "members already locked into another cluster",
                    {"cluster_id": cluster_id, "conflicts": blocked},
                )

            if row is None:
                self._conn.execute(
                    "INSERT INTO clusters(cluster_id, locked, version, created_at)"
                    " VALUES (?,1,1,?)",
                    (cluster_id, _utcnow()),
                )
            else:
                self._conn.execute(
                    "UPDATE clusters SET locked = 1, version = version + 1"
                    " WHERE cluster_id = ?",
                    (cluster_id,),
                )
            # Repoint members at the locked cluster, detaching prior membership.
            self._conn.execute(
                "DELETE FROM memberships WHERE record_id IN ({})".format(
                    ",".join("?" for _ in members)
                ),
                members,
            )
            self._conn.executemany(
                "INSERT INTO memberships(cluster_id, record_id) VALUES (?,?)",
                [(cluster_id, m) for m in members],
            )
            for member in members:
                if prior.get(member) != cluster_id:
                    self._conn.execute(
                        "INSERT INTO audit_events(run_id, operation, record_id, before, after, at)"
                        " VALUES (?,?,?,?,?,?)",
                        (run_id, "lock", member, prior.get(member), cluster_id, _utcnow()),
                    )
            return self.get_cluster(cluster_id)  # type: ignore[return-value]

    def apply_solution(
        self, assignments: dict[str, str], run_id: str
    ) -> dict[str, str | None]:
        """Persist a new assignment for every *unlocked* record.

        Locked memberships are never touched. Empty unlocked clusters are
        removed. Returns the previous assignment (for change-impact diffing).
        """
        with self._lock, self._conn:
            before = self.assignment()
            locked_records = {
                r["record_id"]
                for r in self._conn.execute(
                    "SELECT m.record_id FROM memberships m JOIN clusters c "
                    "ON c.cluster_id = m.cluster_id WHERE c.locked = 1"
                )
            }
            mutable = [rid for rid in assignments if rid not in locked_records]
            changed = [rid for rid in mutable if before.get(rid) != assignments[rid]]
            if changed:
                self._conn.execute(
                    "DELETE FROM memberships WHERE record_id IN ({})".format(
                        ",".join("?" for _ in changed)
                    ),
                    changed,
                )
            self._conn.executemany(
                "INSERT INTO memberships(cluster_id, record_id) VALUES (?,?)",
                [(assignments[rid], rid) for rid in changed],
            )
            # Drop empty, unlocked clusters.
            self._conn.execute(
                "DELETE FROM clusters WHERE locked = 0 AND cluster_id NOT IN"
                " (SELECT cluster_id FROM memberships)"
            )
            for rid in changed:
                self._conn.execute(
                    "INSERT INTO audit_events(run_id, operation, record_id, before, after, at)"
                    " VALUES (?,?,?,?,?,?)",
                    (run_id, "resolve", rid, before.get(rid), assignments[rid], _utcnow()),
                )
            return before

    def ensure_cluster(self, cluster_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO clusters(cluster_id, locked, version, created_at)"
                " VALUES (?,0,1,?)",
                (cluster_id, _utcnow()),
            )

    def max_auto_suffix(self) -> int:
        """Highest N among existing ``cNNNN`` cluster ids (0 if none).

        Returned as a 0-based counter aligned with ``_cluster_id`` (which adds
        1), so passing it straight back yields the next never-used id.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT cluster_id FROM clusters WHERE cluster_id GLOB 'cl[0-9]*'"
            ).fetchall()
        highest = 0
        for row in rows:
            suffix = row["cluster_id"][2:]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        # _cluster_id(index) -> cl{index+1}; to next emit cl{highest+1} the
        # caller's base index must be `highest` (0-based).
        return highest

    # ------------------------------------------------------------------ audit

    def audit(self, run_id: str, operation: str,
              record_id: str | None, before: object, after: object) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_events(run_id, operation, record_id, before, after, at)"
                " VALUES (?,?,?,?,?,?)",
                (
                    run_id,
                    operation,
                    record_id,
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                    _utcnow(),
                ),
            )

    def list_audit(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM audit_events ORDER BY seq DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
