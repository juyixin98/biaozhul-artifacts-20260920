"""SQLite persistence.

Three concerns are kept separate:

* **observations** are append-only raw evidence.  Every batch records the
  calibration version that was active when the data arrived, so historical
  data can always be interpreted with the calibration actually used then.
* **models** are immutable once published.  A new publish closes the previous
  model's validity interval (``valid_to``); exactly one model per device is
  open-ended (``valid_to IS NULL``) at any time.
* **conversions** are logged for audit.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id            TEXT PRIMARY KEY,
    counter_modulus      REAL,
    counter_nominal_hz   REAL,
    created_at           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS models (
    version        TEXT PRIMARY KEY,
    device_id      TEXT NOT NULL,
    status         TEXT NOT NULL,
    payload_json   TEXT NOT NULL,
    signature      TEXT NOT NULL,
    valid_from     REAL NOT NULL,
    valid_to       REAL,
    superseded     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      TEXT NOT NULL,
    ingest_id      TEXT NOT NULL,
    version_used   TEXT,
    t0             REAL NOT NULL,
    t3             REAL NOT NULL,
    c_recv         REAL NOT NULL,
    c_send         REAL,
    seq            INTEGER
);
CREATE TABLE IF NOT EXISTS conversions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id      TEXT NOT NULL,
    version        TEXT NOT NULL,
    counter_in     REAL NOT NULL,
    host_point     REAL,
    bound_lower    REAL,
    bound_upper    REAL,
    status         TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str | Path = "./data/clockdrift.db"):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False + a lock: FastAPI handlers run on a threadpool.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- devices -----------------------------------------------------------

    def upsert_device(self, device_id: str, modulus: Optional[float],
                      nominal_hz: Optional[float]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO devices(device_id, counter_modulus, counter_nominal_hz, created_at)
                   VALUES(?, ?, ?, ?)
                   ON CONFLICT(device_id) DO UPDATE SET
                       counter_modulus = excluded.counter_modulus,
                       counter_nominal_hz = excluded.counter_nominal_hz
                """,
                (device_id, modulus, nominal_hz, utcnow()),
            )
            self._conn.commit()

    def get_device(self, device_id: str) -> Optional[sqlite3.Row]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM devices WHERE device_id = ?", (device_id,))
            return cur.fetchone()

    def count_devices(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) c FROM devices").fetchone()["c"])

    # -- observations ------------------------------------------------------

    def add_observations(self, device_id: str, ingest_id: str,
                         version_used: Optional[str], rows: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(
                """INSERT INTO observations
                       (device_id, ingest_id, version_used, t0, t3, c_recv, c_send, seq)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                [(device_id, ingest_id, version_used, *r) for r in rows],
            )
            self._conn.commit()

    def count_observations(self, device_id: Optional[str] = None) -> int:
        with self._lock:
            if device_id is None:
                return int(self._conn.execute(
                    "SELECT COUNT(*) c FROM observations").fetchone()["c"])
            return int(self._conn.execute(
                "SELECT COUNT(*) c FROM observations WHERE device_id = ?",
                (device_id,)).fetchone()["c"])

    def get_observations(self, device_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(
                """SELECT t0, t3, c_recv, c_send, seq, version_used, ingest_id
                   FROM observations WHERE device_id = ? ORDER BY t0""",
                (device_id,)).fetchall())

    # -- models ------------------------------------------------------------

    def active_version(self, device_id: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT version FROM models WHERE device_id = ? AND valid_to IS NULL",
                (device_id,)).fetchone()
            return row["version"] if row else None

    def publish_model(self, version: str, device_id: str, status: str,
                      payload: dict[str, Any], signature: str,
                      valid_from: float) -> None:
        with self._lock:
            old = self._conn.execute(
                "SELECT version FROM models WHERE device_id = ? AND valid_to IS NULL",
                (device_id,)).fetchall()
            for r in old:
                self._conn.execute(
                    "UPDATE models SET valid_to = ?, superseded = 1 WHERE version = ?",
                    (valid_from, r["version"]))
            self._conn.execute(
                """INSERT INTO models
                       (version, device_id, status, payload_json, signature,
                        valid_from, valid_to, superseded, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, NULL, 0, ?)""",
                (version, device_id, status, json.dumps(payload), signature,
                 valid_from, utcnow()),
            )
            self._conn.commit()

    def get_model(self, version: str) -> Optional[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(
                "SELECT * FROM models WHERE version = ?", (version,)).fetchone()

    def list_models(self, device_id: Optional[str] = None) -> list[sqlite3.Row]:
        with self._lock:
            if device_id:
                cur = self._conn.execute(
                    "SELECT * FROM models WHERE device_id = ? ORDER BY valid_from",
                    (device_id,))
            else:
                cur = self._conn.execute(
                    "SELECT * FROM models ORDER BY device_id, valid_from")
            return list(cur.fetchall())

    def select_model_for_hint(self, device_id: str,
                              hint: Optional[float]) -> Optional[sqlite3.Row]:
        """Pick the calibration version whose validity interval contains hint."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM models WHERE device_id = ? ORDER BY valid_from",
                (device_id,)).fetchall()
        if not rows:
            return None
        if hint is None:
            return next((r for r in rows if r["valid_to"] is None), rows[-1])
        for r in rows:
            if r["valid_from"] <= hint and (r["valid_to"] is None or hint < r["valid_to"]):
                return r
        # Hint outside all known intervals: extrapolate from nearest boundary.
        if hint < rows[0]["valid_from"]:
            return rows[0]
        return rows[-1]

    def count_models(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) c FROM models").fetchone()["c"])

    # -- audit -------------------------------------------------------------

    def log_conversion(self, device_id: str, version: str, counter_in: float,
                       point: Optional[float], lo: Optional[float],
                       hi: Optional[float], status: str) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO conversions
                       (device_id, version, counter_in, host_point,
                        bound_lower, bound_upper, status, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (device_id, version, counter_in, point, lo, hi, status, utcnow()),
            )
            self._conn.commit()
