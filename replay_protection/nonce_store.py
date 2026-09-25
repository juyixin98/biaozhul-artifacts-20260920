"""Atomic nonce registry backed by SQLite.

The UNIQUE(kid, nonce) constraint makes "check then insert" a single atomic
SQL statement (INSERT ... ON CONFLICT DO NOTHING + rowcount): under concurrent
threads or processes sharing one database file, the second commit of the same
nonce fails and the request is rejected as a replay.

Rows carry ``expires_at`` (kid timestamp + replay window) and are purged by a
background daemon thread and opportunistically before each insert.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_nonces (
    kid        TEXT NOT NULL,
    nonce      TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (kid, nonce)
);
"""


class NonceStore:
    def __init__(self, db_path: str | None = ":memory:", window_seconds: int = 300):
        self.window = window_seconds
        self._lock = threading.Lock()
        if db_path == ":memory:":
            # check_same_thread=False is safe under self._lock; every call opens
            # its own short-lived connection anyway.
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=MEMORY")
            self._file = None
        else:
            self._conn = None
            self._file = str(db_path)
            self._init_file()
        self._get().executescript(_SCHEMA)
        self._get().commit()

    def _connect(self) -> sqlite3.Connection:
        if self._file is None:
            return self._conn
        conn = sqlite3.connect(self._file, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_file(self) -> None:
        Path(self._file).parent.mkdir(parents=True, exist_ok=True)

    def _get(self) -> sqlite3.Connection:
        return self._connect()

    def purge_expired(self, now: int | None = None) -> int:
        now = int(time.time()) if now is None else int(now)
        with self._lock:
            conn = self._get()
            try:
                cur = conn.execute(
                    "DELETE FROM seen_nonces WHERE expires_at < ?", (now,)
                )
                conn.commit()
                return cur.rowcount
            finally:
                if self._file is not None:
                    conn.close()

    def register(self, kid: str, nonce: str, timestamp: int, now: int | None = None) -> bool:
        """Atomically claim (kid, nonce).

        Returns True if this call uniquely registered the nonce, False if it
        was already present. Expired rows for the same kid are removed first,
        so a nonce can be reused once its window closes.
        """
        now = int(time.time()) if now is None else int(now)
        expires_at = int(timestamp) + self.window
        with self._lock:
            conn = self._get()
            try:
                # Opportunistic purge scoped to this kid keeps the table bounded.
                conn.execute(
                    "DELETE FROM seen_nonces WHERE kid = ? AND expires_at < ?",
                    (kid, now),
                )
                cur = conn.execute(
                    "INSERT OR IGNORE INTO seen_nonces (kid, nonce, expires_at) "
                    "VALUES (?, ?, ?)",
                    (kid, nonce, expires_at),
                )
                conn.commit()
                return cur.rowcount == 1
            finally:
                if self._file is not None:
                    conn.close()

    def count(self) -> int:
        with self._lock:
            conn = self._get()
            try:
                (n,) = conn.execute("SELECT COUNT(*) FROM seen_nonces").fetchone()
                return n
            finally:
                if self._file is not None:
                    conn.close()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


def start_purge_thread(store: NonceStore, interval: float = 60.0) -> threading.Thread:
    """Daemon thread that periodically removes expired nonces."""

    def _run() -> None:
        while True:
            time.sleep(interval)
            store.purge_expired()

    t = threading.Thread(target=_run, name="nonce-purge", daemon=True)
    t.start()
    return t
