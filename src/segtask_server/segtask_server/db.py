"""SQLite persistence layer - the single source of truth.

Concurrency/consistency rules enforced here (not in the ROS layer):

* ``goals.goal_id`` is a PRIMARY KEY. ``register_goal`` does an atomic
  insert-or-fetch so duplicate goal IDs are decided by SQLite, not by
  in-process check-then-act.
* Every status change is a *compare-and-set*: ``UPDATE ... WHERE id=? AND
  status IN (...)``. A cancel racing with the last segment commit can produce
  only one terminal row, because exactly one of the competing CAS statements
  matches.
* Segment commits are accepted only while the goal is RUNNING or RECOVERING.
  A CANCELING/CANCELED goal can never have another segment result committed,
  even if the worker thread is already inside the segment loop.
* All state changes append an HMAC-chained event inside the SAME transaction
  (see :mod:`segtask_server.crypto`), so an audit can prove which transitions
  actually happened and detect tampering.
"""

from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from . import status as st
from .crypto import GENESIS, canonical_payload, chain_hash

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    goal_id               TEXT PRIMARY KEY,
    status                INTEGER NOT NULL,
    segments_total        INTEGER NOT NULL,
    segments_done         INTEGER NOT NULL DEFAULT 0,
    work_units            INTEGER NOT NULL,
    checkpoint_ms         INTEGER NOT NULL,
    policy                TEXT NOT NULL,
    params_hash           TEXT NOT NULL,
    result_hash           TEXT,
    result_message        TEXT,
    cancel_requested      INTEGER NOT NULL DEFAULT 0,
    resumed_after_restart INTEGER NOT NULL DEFAULT 0,
    run_id                TEXT NOT NULL,
    created_at_ns         INTEGER NOT NULL,
    updated_at_ns         INTEGER NOT NULL,
    started_at_ns         INTEGER,
    ended_at_ns           INTEGER
);
CREATE TABLE IF NOT EXISTS segments (
    goal_id         TEXT NOT NULL,
    segment_index   INTEGER NOT NULL,
    output_hash     TEXT NOT NULL,
    work_units      INTEGER NOT NULL,
    run_id          TEXT NOT NULL,
    committed_at_ns INTEGER NOT NULL,
    PRIMARY KEY (goal_id, segment_index)
);
CREATE TABLE IF NOT EXISTS goal_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns       INTEGER NOT NULL,
    goal_id     TEXT,
    run_id      TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    event_hash  TEXT NOT NULL
);
"""

# statuses in which a new segment result may be committed.
_COMMIT_OK = (st.RUNNING, st.RECOVERING)


def now_ns() -> int:
    return time.time_ns()


class Store:
    """Thread-safe wrapper around one SQLite database file."""

    def __init__(self, db_path: str, secret: bytes, run_id: Optional[str] = None):
        self.db_path = db_path
        self.secret = secret
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute('PRAGMA journal_mode=WAL')
        self._conn.execute('PRAGMA foreign_keys=ON')
        self._conn.execute('PRAGMA busy_timeout=5000')
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._tip = self._load_tip()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ---------------------------------------------------------------- basics

    def _load_tip(self) -> str:
        cur = self._conn.execute(
            'SELECT event_hash FROM goal_events ORDER BY id DESC LIMIT 1')
        row = cur.fetchone()
        return row['event_hash'] if row else GENESIS

    def _append_event(self, conn, event_type: str, goal_id: Optional[str],
                      details: dict[str, Any]) -> str:
        """Append a chained event. Caller MUST hold the write transaction."""
        payload: dict[str, Any] = {'event_type': event_type}
        if goal_id is not None:
            payload['goal_id'] = goal_id
        payload.update(details)
        payload_json = canonical_payload(payload)
        event_hash = chain_hash(self.secret, self._tip, payload)
        ts = now_ns()
        conn.execute(
            'INSERT INTO goal_events (ts_ns, goal_id, run_id, event_type, '
            'payload_json, prev_hash, event_hash) VALUES (?,?,?,?,?,?,?)',
            (ts, goal_id, self.run_id, event_type, payload_json,
             self._tip, event_hash))
        self._tip = event_hash
        return event_hash

    def append_server_event(self, event_type: str, details: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                self._append_event(self._conn, event_type, None, details)
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def get_goal(self, goal_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                'SELECT * FROM goals WHERE goal_id=?', (goal_id,))
            return cur.fetchone()

    def get_segments(self, goal_id: str) -> list[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                'SELECT * FROM segments WHERE goal_id=? ORDER BY segment_index',
                (goal_id,))
            return cur.fetchall()

    def list_goals(self, include_finished: bool, limit: int = 200) -> list[sqlite3.Row]:
        sql = 'SELECT * FROM goals'
        if not include_finished:
            placeholders = ','.join('?' for _ in st.TERMINAL_STATUSES)
            sql += f' WHERE status NOT IN ({placeholders})'
            params: tuple[Any, ...] = st.TERMINAL_STATUSES
        else:
            params = ()
        sql += ' ORDER BY created_at_ns DESC LIMIT ?'
        with self._lock:
            return self._conn.execute(sql, (*params, limit)).fetchall()

    # ------------------------------------------------------------- admission

    def register_goal(self, goal_id: str, segments_total: int, work_units: int,
                      checkpoint_ms: int, policy: str, params_hash: str
                      ) -> tuple[sqlite3.Row, bool]:
        """Atomic idempotent goal registration.

        Returns ``(row, created)`` where ``created`` is True only for the
        first registration. Relying on the PRIMARY KEY makes duplicate
        submission safe even with two threads racing.
        """
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                existing = self._conn.execute(
                    'SELECT * FROM goals WHERE goal_id=?', (goal_id,)).fetchone()
                if existing is not None:
                    self._conn.rollback()
                    return existing, False
                ts = now_ns()
                self._conn.execute(
                    'INSERT INTO goals (goal_id, status, segments_total, '
                    'segments_done, work_units, checkpoint_ms, policy, '
                    'params_hash, run_id, created_at_ns, updated_at_ns, '
                    'started_at_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (goal_id, st.PENDING, segments_total, 0, work_units,
                     checkpoint_ms, policy, params_hash, self.run_id, ts, ts, ts))
                self._append_event(self._conn, 'GOAL_ACCEPTED', goal_id, {
                    'segments_total': segments_total,
                    'work_units': work_units,
                    'checkpoint_ms': checkpoint_ms,
                    'policy': policy,
                    'params_hash': params_hash,
                })
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self.get_goal(goal_id), True

    # --------------------------------------------------------- CAS transitions

    def transition(self, goal_id: str, expected: tuple[int, ...], new_status: int,
                   event_type: str, message: Optional[str] = None,
                   set_result: bool = False, mark_recovered: bool = False
                   ) -> Optional[sqlite3.Row]:
        """Compare-and-set status. Returns the updated row, or None on loss.

        The UPDATE matches only while the status is in ``expected``; that
        single atomic statement is the cancel/complete race arbiter.
        """
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                placeholders = ','.join('?' for _ in expected)
                ts = now_ns()
                sets = ['status=?', 'updated_at_ns=?']
                params: list[Any] = [new_status, ts]
                if message is not None:
                    sets.append('result_message=?')
                    params.append(message)
                if set_result:
                    sets.append('ended_at_ns=?')
                    params.append(ts)
                if mark_recovered:
                    sets.append('resumed_after_restart=1')
                sql = (f"UPDATE goals SET {', '.join(sets)} "
                       f"WHERE goal_id=? AND status IN ({placeholders})")
                params.extend([goal_id, *expected])
                cur = self._conn.execute(sql, params)
                if cur.rowcount != 1:
                    self._conn.rollback()
                    return None
                self._append_event(self._conn, event_type, goal_id,
                                   {'new_status': new_status,
                                    'status_name': st.STATUS_NAMES[new_status]})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self.get_goal(goal_id)

    def mark_running(self, goal_id: str) -> Optional[sqlite3.Row]:
        return self.transition(
            goal_id, (st.PENDING, st.RECOVERING), st.RUNNING, 'GOAL_STARTED')

    def request_cancel(self, goal_id: str) -> tuple[bool, str]:
        """Durably record a cancel request.

        RUNNING/PENDING/RECOVERING -> CANCELING (sticky cancel_requested=1).
        Already CANCELING is reported as accepted (idempotent). Terminal goals
        cannot be canceled.
        """
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                row = self._conn.execute(
                    'SELECT status FROM goals WHERE goal_id=?',
                    (goal_id,)).fetchone()
                if row is None:
                    self._conn.rollback()
                    return False, 'unknown goal'
                current = row['status']
                if current in st.TERMINAL_STATUSES:
                    self._conn.rollback()
                    return False, f'goal already terminal ({st.STATUS_NAMES[current]})'
                if current == st.CANCELING:
                    self._conn.rollback()
                    return True, 'cancel already requested'
                cur = self._conn.execute(
                    'UPDATE goals SET status=?, cancel_requested=1, updated_at_ns=? '
                    'WHERE goal_id=? AND status IN (?,?,?)',
                    (st.CANCELING, now_ns(), goal_id,
                     st.RUNNING, st.PENDING, st.RECOVERING))
                if cur.rowcount != 1:
                    self._conn.rollback()
                    return False, 'status changed concurrently'
                self._append_event(self._conn, 'CANCEL_REQUESTED', goal_id,
                                   {'previous_status': current})
                self._conn.commit()
                return True, 'cancel recorded'
            except Exception:
                self._conn.rollback()
                raise

    def admin_abort(self, goal_id: str) -> tuple[bool, str]:
        """Explicit operator abort; only non-terminal goals may be aborted.

        Primarily for goals parked in RECOVERING under manual recovery mode.
        """
        row = self.transition(
            goal_id, st.NON_TERMINAL_STATUSES, st.ABORTED, 'ADMIN_ABORTED',
            message='aborted by explicit operator request', set_result=True)
        return (row is not None, 'aborted' if row else 'goal not abortable')

    # ------------------------------------------------------------ segment I/O

    def commit_segment(self, goal_id: str, segment_index: int, output_hash: str
                       ) -> tuple[bool, str, Optional[sqlite3.Row]]:
        """Commit one segment result.

        Rejects out-of-order indexes and every goal status except RUNNING /
        RECOVERING - this is what stops a canceled goal from receiving more
        segment results. The status guard, segment insert and goal update are
        one transaction, so the count and the segment rows can never diverge.
        """
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                row = self._conn.execute(
                    'SELECT * FROM goals WHERE goal_id=?', (goal_id,)).fetchone()
                if row is None:
                    self._conn.rollback()
                    return False, 'unknown goal', None
                if row['status'] not in _COMMIT_OK:
                    self._conn.rollback()
                    return False, (f'commit rejected: goal status is '
                                   f'{st.STATUS_NAMES[row["status"]]}'), None
                if segment_index != row['segments_done']:
                    self._conn.rollback()
                    return False, (f'commit rejected: expected segment index '
                                   f'{row["segments_done"]}, got {segment_index}'), None
                self._conn.execute(
                    'INSERT INTO segments (goal_id, segment_index, output_hash, '
                    'work_units, run_id, committed_at_ns) VALUES (?,?,?,?,?,?)',
                    (goal_id, segment_index, output_hash, row['work_units'],
                     self.run_id, now_ns()))
                done = segment_index + 1
                self._conn.execute(
                    'UPDATE goals SET segments_done=?, result_hash=?, updated_at_ns=? '
                    'WHERE goal_id=?',
                    (done, output_hash, now_ns(), goal_id))
                self._append_event(self._conn, 'SEGMENT_COMMITTED', goal_id, {
                    'segment_index': segment_index,
                    'segments_done': done,
                    'output_hash': output_hash,
                })
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                self._conn.rollback()
                return False, f'commit rejected: {exc}', None
            except Exception:
                self._conn.rollback()
                raise
            return True, 'committed', self.get_goal(goal_id)

    # ----------------------------------------------------------- finalization

    def finalize(self, goal_id: str) -> Optional[sqlite3.Row]:
        """Decide the single terminal state after the worker loop ends.

        Explicit arbitration policy:
        * CANCELING + cancel_requested wins over completion -> CANCELED, even
          though all segment outputs may already be durable. The durable count
          (not the status) tells what work actually finished.
        * RUNNING/RECOVERING with all segments done -> SUCCEEDED.
        """
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                row = self._conn.execute(
                    'SELECT * FROM goals WHERE goal_id=?', (goal_id,)).fetchone()
                if row is None or row['status'] in st.TERMINAL_STATUSES:
                    self._conn.rollback()
                    return row
                ts = now_ns()
                if row['status'] == st.CANCELING or row['cancel_requested']:
                    cur = self._conn.execute(
                        'UPDATE goals SET status=?, updated_at_ns=?, ended_at_ns=?, '
                        "result_message=? WHERE goal_id=? AND status NOT IN "
                        f'({",".join("?" for _ in st.TERMINAL_STATUSES)})',
                        (st.CANCELED, ts, ts,
                         f'canceled; {row["segments_done"]}/{row["segments_total"]} '
                         'segments durably complete',
                         goal_id, *st.TERMINAL_STATUSES))
                    event_type = 'GOAL_CANCELED'
                    new_status = st.CANCELED
                elif (row['segments_done'] >= row['segments_total']
                      and row['status'] in (st.RUNNING, st.RECOVERING)):
                    cur = self._conn.execute(
                        'UPDATE goals SET status=?, updated_at_ns=?, ended_at_ns=?, '
                        "result_message=? WHERE goal_id=? AND status IN (?,?)",
                        (st.SUCCEEDED, ts, ts, 'all segments complete',
                         goal_id, st.RUNNING, st.RECOVERING))
                    event_type = 'GOAL_SUCCEEDED'
                    new_status = st.SUCCEEDED
                else:
                    # Not canceling, but segments incomplete: treat as aborted
                    # (worker ended without finishing and without cancellation).
                    cur = self._conn.execute(
                        'UPDATE goals SET status=?, updated_at_ns=?, ended_at_ns=?, '
                        "result_message=? WHERE goal_id=? AND status IN (?,?)",
                        (st.ABORTED, ts, ts,
                         f'worker ended early: {row["segments_done"]}/'
                         f'{row["segments_total"]} segments',
                         goal_id, st.RUNNING, st.RECOVERING))
                    event_type = 'GOAL_ABORTED'
                    new_status = st.ABORTED
                if cur.rowcount != 1:
                    self._conn.rollback()
                    return None
                self._append_event(self._conn, event_type, goal_id,
                                   {'new_status': new_status,
                                    'segments_done': row['segments_done'],
                                    'segments_total': row['segments_total']})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            return self.get_goal(goal_id)

    # -------------------------------------------------------------- recovery

    def startup_recovery(self, mode: str) -> list[sqlite3.Row]:
        """Mark goals from previous runs RECOVERING and apply the policy.

        * Any non-terminal goal (PENDING/RUNNING/CANCELING) becomes
          RECOVERING, and a ``RECOVERY_MARKED`` event is appended.
        * Goals whose own policy is ``abort`` (or node mode ``abort``) are
          immediately transitioned to ABORTED.
        * Under ``resume`` mode, ``resume`` goals are returned for immediate
          background continuation; under ``manual`` they stay RECOVERING until
          an AdminRequest arrives.
        * ``cancel_requested`` survives the restart: a cancel already recorded
          is never forgotten.
        """
        assert mode in st.VALID_MODES
        resumed: list[sqlite3.Row] = []
        with self._lock:
            self._conn.execute('BEGIN IMMEDIATE')
            try:
                rows = self._conn.execute(
                    'SELECT * FROM goals WHERE status NOT IN '
                    f'({",".join("?" for _ in st.TERMINAL_STATUSES)})',
                    st.TERMINAL_STATUSES).fetchall()
                for row in rows:
                    self._conn.execute(
                        'UPDATE goals SET status=?, resumed_after_restart=1, '
                        'run_id=?, updated_at_ns=? WHERE goal_id=? AND status NOT IN '
                        f'({",".join("?" for _ in st.TERMINAL_STATUSES)})',
                        (st.RECOVERING, self.run_id, now_ns(), row['goal_id'],
                         *st.TERMINAL_STATUSES))
                    self._append_event(self._conn, 'RECOVERY_MARKED', row['goal_id'],
                                       {'previous_status': row['status'],
                                        'mode': mode})
                    should_abort = (mode == st.POLICY_ABORT
                                    or row['policy'] == st.POLICY_ABORT)
                    if should_abort:
                        self._conn.execute(
                            'UPDATE goals SET status=?, updated_at_ns=?, ended_at_ns=?, '
                            'result_message=? WHERE goal_id=?',
                            (st.ABORTED, now_ns(), now_ns(),
                             'aborted by recovery policy after restart',
                             row['goal_id']))
                        self._append_event(self._conn, 'RECOVERY_ABORTED',
                                           row['goal_id'], {'mode': mode})
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            if mode != st.MODE_MANUAL:
                for row in rows:
                    fresh = self.get_goal(row['goal_id'])
                    if fresh is not None and fresh['status'] == st.RECOVERING:
                        resumed.append(fresh)
        return resumed

    # ------------------------------------------------------------- diagnostics

    def events(self, goal_id: Optional[str] = None) -> list[sqlite3.Row]:
        with self._lock:
            if goal_id is None:
                return self._conn.execute(
                    'SELECT * FROM goal_events ORDER BY id').fetchall()
            return self._conn.execute(
                'SELECT * FROM goal_events WHERE goal_id=? ORDER BY id',
                (goal_id,)).fetchall()

    def last_event_hash(self) -> str:
        with self._lock:
            return self._tip
