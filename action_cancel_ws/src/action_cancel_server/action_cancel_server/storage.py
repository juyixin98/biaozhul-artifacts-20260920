"""Durable storage layer (SQLite) with the state-machine transitions.

Concurrency model
-----------------
* One SQLite connection per server process, WAL journal, a busy timeout and a
  process-local re-entrant lock.
* Every state transition is a **conditional** ``UPDATE ... WHERE state IN
  (...)`` inside a ``BEGIN IMMEDIATE`` transaction. SQLite's write lock is the
  single arbiter of the cancel-vs-complete race: the two transactions
  serialize, and exactly one terminal state can ever be committed.
* Segment progress is inserted in the same transaction style and rejected if
  the goal already left the active states — a canceled goal can therefore
  never receive another segment row ("already-canceled goals cannot submit
  segment results").

Schema version is stored in ``meta`` so an incompatible database is refused
instead of silently corrupted.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import states

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class GoalRow:
    goal_id: str
    state: int
    total_segments: int
    completed_segments: int
    iterations: int
    policy: int
    fingerprint: str
    result_hash: str = ""
    error_code: str = ""
    last_rejection_code: str = ""
    inputs: tuple[str, ...] = field(default_factory=tuple)
    created_at: float = 0.0
    updated_at: float = 0.0


@dataclass(frozen=True)
class SegmentRow:
    goal_id: str
    segment_index: int
    segment_hash: str
    chained_hash: str
    input_fingerprint: str
    duration_ms: float
    recovered: bool


@dataclass(frozen=True)
class CommitResult:
    status: str                 # "committed" | "already_committed" | "rejected"
    state: int                  # goal state observed inside the transaction
    completed_segments: int
    row: Optional[SegmentRow] = None


class Storage:
    def __init__(self, db_path: str | Path, busy_timeout_ms: int = 5000):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            isolation_level=None,  # explicit BEGIN/COMMIT
            timeout=busy_timeout_ms / 1000.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._busy_timeout_ms = busy_timeout_ms
        self._configure()
        self._migrate()

    # ------------------------------------------------------------------ setup
    def _configure(self) -> None:
        c = self._conn
        c.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=FULL")  # durability across power loss
        c.execute("PRAGMA foreign_keys=ON")

    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS goals (
                    goal_id            TEXT PRIMARY KEY,
                    state              INTEGER NOT NULL,
                    total_segments     INTEGER NOT NULL,
                    completed_segments INTEGER NOT NULL DEFAULT 0,
                    iterations         INTEGER NOT NULL,
                    policy             INTEGER NOT NULL,
                    fingerprint        TEXT NOT NULL,
                    inputs_json        TEXT NOT NULL,
                    result_hash        TEXT NOT NULL DEFAULT '',
                    error_code         TEXT NOT NULL DEFAULT '',
                    last_rejection_code TEXT NOT NULL DEFAULT '',
                    created_at         REAL NOT NULL,
                    updated_at         REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS segments (
                    goal_id           TEXT NOT NULL,
                    segment_index     INTEGER NOT NULL,
                    segment_hash      TEXT NOT NULL,
                    chained_hash      TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    duration_ms       REAL NOT NULL,
                    recovered         INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (goal_id, segment_index),
                    FOREIGN KEY (goal_id) REFERENCES goals(goal_id)
                );

                CREATE TABLE IF NOT EXISTS rejections (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    goal_id     TEXT NOT NULL,
                    fingerprint TEXT NOT NULL DEFAULT '',
                    reason      TEXT NOT NULL,
                    created_at  REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS ix_segments_goal
                    ON segments(goal_id, segment_index);
                CREATE INDEX IF NOT EXISTS ix_goals_state ON goals(state);
                CREATE INDEX IF NOT EXISTS ix_rejections_goal ON rejections(goal_id);
                """
            )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"schema version mismatch: db={row['value']} "
                    f"code={SCHEMA_VERSION}"
                )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _rollback_safely(self) -> None:
        """ROLLBACK if a transaction is active.

        Some code paths roll back explicitly before raising; this keeps the
        ``except`` blocks idempotent instead of failing with
        "no transaction is active".
        """
        if self._conn.in_transaction:
            self._conn.execute("ROLLBACK")

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _row_to_goal(r: sqlite3.Row) -> GoalRow:
        return GoalRow(
            goal_id=r["goal_id"],
            state=r["state"],
            total_segments=r["total_segments"],
            completed_segments=r["completed_segments"],
            iterations=r["iterations"],
            policy=r["policy"],
            fingerprint=r["fingerprint"],
            result_hash=r["result_hash"],
            error_code=r["error_code"],
            last_rejection_code=r["last_rejection_code"],
            inputs=tuple(json.loads(r["inputs_json"])),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    # ------------------------------------------------------------- goal CRUD
    def get_goal(self, goal_id: str) -> Optional[GoalRow]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM goals WHERE goal_id=?", (goal_id,)
            ).fetchone()
            return self._row_to_goal(r) if r else None

    def create_goal(
        self,
        goal_id: str,
        inputs: list[str],
        iterations: int,
        policy: int,
        fingerprint: str,
    ) -> tuple[str, Optional[GoalRow]]:
        """Insert a fresh goal in PENDING.

        Returns ``(status, existing)`` where status is one of:
        ``"created"``, ``"duplicate_active"``, ``"duplicate_recoverable"``,
        ``"duplicate_terminal"``. Caller maps those to accept/reject and,
        on parameter mismatch, records a rejection.
        """
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                r = self._conn.execute(
                    "SELECT * FROM goals WHERE goal_id=?", (goal_id,)
                ).fetchone()
                if r is not None:
                    self._conn.execute("ROLLBACK")
                    return "duplicate_" + _dup_kind(r["state"]), self._row_to_goal(r)
                self._conn.execute(
                    """
                    INSERT INTO goals(goal_id, state, total_segments,
                        completed_segments, iterations, policy, fingerprint,
                        inputs_json, result_hash, error_code,
                        last_rejection_code, created_at, updated_at)
                    VALUES(?, ?, ?, 0, ?, ?, ?, ?, '', '', '', ?, ?)
                    """,
                    (
                        goal_id,
                        states.PENDING,
                        len(inputs),
                        int(iterations),
                        int(policy),
                        fingerprint,
                        json.dumps(list(inputs), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_safely()
                raise
            return "created", self.get_goal(goal_id)

    def mark_running(self, goal_id: str) -> bool:
        """PENDING -> RUNNING. False if the goal already moved on."""
        return self._transition(
            goal_id,
            allowed=(states.PENDING,),
            new_state=states.RUNNING,
        )

    # --------------------------------------------------------- segment commits
    def commit_segment(
        self,
        goal_id: str,
        segment_index: int,
        segment_hash: str,
        chained_hash: str,
        input_fingerprint: str,
        duration_ms: float,
        recovered: bool = False,
        final_result_hash: str = "",
    ) -> CommitResult:
        """Durably commit one completed segment.

        * rejected when the goal is not in an executable state (canceled
          goals can never submit results);
        * ``already_committed`` for an idempotent re-delivery of the same
          index (only if hashes match);
        * when this is the final segment the goal is marked SUCCEEDED in the
          **same transaction**, so "last segment committed" and "goal
          terminal" are atomic vs. a concurrent cancel.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                g = self._conn.execute(
                    "SELECT * FROM goals WHERE goal_id=?", (goal_id,)
                ).fetchone()
                if g is None:
                    self._conn.execute("ROLLBACK")
                    return CommitResult("rejected", states.ABORTED, 0)
                state = g["state"]

                existing = self._conn.execute(
                    "SELECT * FROM segments WHERE goal_id=? AND segment_index=?",
                    (goal_id, segment_index),
                ).fetchone()
                if existing is not None:
                    # Idempotent redelivery of the same result must be
                    # acknowledged regardless of whether the goal has since
                    # become terminal (the durable copy is identical).
                    self._conn.execute("ROLLBACK")
                    if (
                        existing["segment_hash"] != segment_hash
                        or existing["chained_hash"] != chained_hash
                    ):
                        raise RuntimeError(
                            f"conflicting segment result for {goal_id}#"
                            f"{segment_index}"
                        )
                    return CommitResult(
                        "already_committed",
                        state,
                        g["completed_segments"],
                        row=SegmentRow(
                            goal_id,
                            segment_index,
                            segment_hash,
                            chained_hash,
                            existing["input_fingerprint"],
                            existing["duration_ms"],
                            bool(existing["recovered"]),
                        ),
                    )

                # No durable copy exists yet: a fresh result is accepted only
                # while the goal is executable.
                if state not in (states.RUNNING, states.RECOVERABLE):
                    self._conn.execute("ROLLBACK")
                    return CommitResult(
                        "rejected", state, g["completed_segments"]
                    )

                expected_index = g["completed_segments"]
                if segment_index != expected_index:
                    self._conn.execute("ROLLBACK")
                    raise RuntimeError(
                        f"out-of-order segment {segment_index} for {goal_id}: "
                        f"expected {expected_index}"
                    )

                now = time.time()
                self._conn.execute(
                    """
                    INSERT INTO segments(goal_id, segment_index, segment_hash,
                        chained_hash, input_fingerprint, duration_ms, recovered)
                    VALUES(?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        goal_id,
                        segment_index,
                        segment_hash,
                        chained_hash,
                        input_fingerprint,
                        float(duration_ms),
                        1 if recovered else 0,
                    ),
                )
                new_completed = expected_index + 1
                self._conn.execute(
                    "UPDATE goals SET completed_segments=?, updated_at=? "
                    "WHERE goal_id=?",
                    (new_completed, now, goal_id),
                )
                final_state = state
                if new_completed >= g["total_segments"]:
                    cur = self._conn.execute(
                        "UPDATE goals SET state=?, result_hash=?, "
                        "error_code='', updated_at=? "
                        "WHERE goal_id=? AND state IN (?, ?)",
                        (
                            states.SUCCEEDED,
                            chained_hash,
                            now,
                            goal_id,
                            states.RUNNING,
                            states.RECOVERABLE,
                        ),
                    )
                    if cur.rowcount != 1:
                        # Lost the race against a concurrent cancel: drop the
                        # segment insert so the durable count matches CANCELED.
                        self._conn.execute("ROLLBACK")
                        g2 = self._conn.execute(
                            "SELECT state, completed_segments FROM goals "
                            "WHERE goal_id=?",
                            (goal_id,),
                        ).fetchone()
                        return CommitResult(
                            "rejected", g2["state"], g2["completed_segments"]
                        )
                    final_state = states.SUCCEEDED
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_safely()
                raise

            row = self.get_segment(goal_id, segment_index)
            return CommitResult("committed", final_state, new_completed, row=row)

    def get_segment(self, goal_id: str, index: int) -> Optional[SegmentRow]:
        with self._lock:
            r = self._conn.execute(
                "SELECT * FROM segments WHERE goal_id=? AND segment_index=?",
                (goal_id, index),
            ).fetchone()
            return self._row_to_segment(r) if r else None

    def get_segments(self, goal_id: str) -> list[SegmentRow]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM segments WHERE goal_id=? ORDER BY segment_index",
                (goal_id,),
            ).fetchall()
            return [self._row_to_segment(r) for r in rows]

    @staticmethod
    def _row_to_segment(r: sqlite3.Row) -> SegmentRow:
        return SegmentRow(
            goal_id=r["goal_id"],
            segment_index=r["segment_index"],
            segment_hash=r["segment_hash"],
            chained_hash=r["chained_hash"],
            input_fingerprint=r["input_fingerprint"],
            duration_ms=r["duration_ms"],
            recovered=bool(r["recovered"]),
        )

    # ------------------------------------------------------------ transitions
    def _transition(
        self,
        goal_id: str,
        allowed: tuple[int, ...],
        new_state: int,
        error_code: str = "",
        result_hash: str | None = None,
    ) -> tuple[bool, int]:
        """Conditional state transition.

        Returns ``(changed, observed_state)``. The ``WHERE state IN (...)``
        predicate is the atomic compare-and-set; rowcount==1 means this call
        won the transition.
        """
        placeholders = ",".join("?" for _ in allowed)
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                r = self._conn.execute(
                    "SELECT state FROM goals WHERE goal_id=?", (goal_id,)
                ).fetchone()
                if r is None:
                    self._conn.execute("ROLLBACK")
                    return False, states.ABORTED
                observed = r["state"]
                if observed not in allowed:
                    self._conn.execute("ROLLBACK")
                    return False, observed
                if result_hash is not None:
                    cur = self._conn.execute(
                        f"UPDATE goals SET state=?, result_hash=?, "
                        f"error_code=?, updated_at=? "
                        f"WHERE goal_id=? AND state IN ({placeholders})",
                        (new_state, result_hash, error_code, now, goal_id, *allowed),
                    )
                else:
                    cur = self._conn.execute(
                        f"UPDATE goals SET state=?, error_code=?, updated_at=? "
                        f"WHERE goal_id=? AND state IN ({placeholders})",
                        (new_state, error_code, now, goal_id, *allowed),
                    )
                changed = cur.rowcount == 1
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_safely()
                raise
            return changed, (new_state if changed else observed)

    def request_cancel(self, goal_id: str) -> tuple[str, int]:
        """Durably admit a cancel request.

        Returns ``("canceled", CANCELED)`` when this call won and the goal is
        now durably CANCELED, or ``("already_terminal", state)`` /
        ``("not_found", ABORTED)``. PENDING/RUNNING/RECOVERABLE goals can be
        canceled; a SUCCEEDED goal can never flip back.
        """
        with self._lock:
            changed, observed = self._transition(
                goal_id,
                allowed=(states.PENDING, states.RUNNING, states.RECOVERABLE),
                new_state=states.CANCELED,
                error_code=states.ERR_CANCEL_DURING_EXECUTION,
            )
            if changed:
                return "canceled", states.CANCELED
            if self.get_goal(goal_id) is None:
                return "not_found", states.ABORTED
            return "already_terminal", observed

    def abort(self, goal_id: str, error_code: str) -> tuple[bool, int]:
        return self._transition(
            goal_id,
            allowed=(states.PENDING, states.RUNNING, states.RECOVERABLE),
            new_state=states.ABORTED,
            error_code=error_code,
        )

    # ------------------------------------------------------------- recovery
    def mark_all_running_recoverable(self) -> list[str]:
        """Startup sweep: PENDING/RUNNING -> RECOVERABLE.

        Anything that was alive when the previous process died is now
        explicitly *recoverable* (never silently resumed or forgotten).
        """
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(
                    "SELECT goal_id FROM goals WHERE state IN (?, ?)",
                    (states.PENDING, states.RUNNING),
                ).fetchall()
                ids = [r["goal_id"] for r in rows]
                self._conn.execute(
                    "UPDATE goals SET state=?, updated_at=? "
                    "WHERE state IN (?, ?)",
                    (states.RECOVERABLE, now, states.PENDING, states.RUNNING),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_safely()
                raise
            return ids

    def recover_begin(self, goal_id: str) -> tuple[bool, int]:
        """RECOVERABLE -> RUNNING (a resume won the policy decision)."""
        return self._transition(
            goal_id,
            allowed=(states.RECOVERABLE,),
            new_state=states.RUNNING,
        )

    def recover_abort(self, goal_id: str) -> tuple[bool, int]:
        """RECOVERABLE -> ABORTED (POLICY_ABORT or recovery failure)."""
        return self._transition(
            goal_id,
            allowed=(states.RECOVERABLE,),
            new_state=states.ABORTED,
            error_code=states.ERR_ABORT_POLICY,
        )

    def mark_segments_recovered(self, goal_id: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE segments SET recovered=1 WHERE goal_id=? AND recovered=0",
                (goal_id,),
            )
            return cur.rowcount

    # ------------------------------------------------------------ rejections
    def record_rejection(
        self, goal_id: str, fingerprint: str, reason: str
    ) -> None:
        """Persist a rejected send attempt (e.g. same id, other parameters).

        Rejected *attempts* never create a goal row; they land here so an
        auditor can see the full history. The last reason is also stamped on
        an existing goal row.
        """
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "INSERT INTO rejections(goal_id, fingerprint, reason, "
                    "created_at) VALUES(?, ?, ?, ?)",
                    (goal_id, fingerprint, reason, now),
                )
                self._conn.execute(
                    "UPDATE goals SET last_rejection_code=?, updated_at=? "
                    "WHERE goal_id=?",
                    (reason, now, goal_id),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                self._rollback_safely()
                raise

    def get_rejections(self, goal_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT goal_id, fingerprint, reason, created_at "
                "FROM rejections WHERE goal_id=? ORDER BY id",
                (goal_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------------------------------------------------------------- queries
    def list_goals(self, state_filter: int | None = None) -> list[GoalRow]:
        with self._lock:
            if state_filter is None or int(state_filter) >= 200:
                rows = self._conn.execute(
                    "SELECT * FROM goals ORDER BY created_at, goal_id"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM goals WHERE state=? "
                    "ORDER BY created_at, goal_id",
                    (int(state_filter),),
                ).fetchall()
            return [self._row_to_goal(r) for r in rows]


def _dup_kind(state: int) -> str:
    if state == states.RECOVERABLE:
        return "recoverable"
    if state in states.TERMINAL:
        return "terminal"
    return "active"
