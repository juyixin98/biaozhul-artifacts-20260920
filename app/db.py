"""SQLite persistence for immutable pool snapshots and quote records.

Storage is write-once per id: pools and quotes are never updated or deleted
through the API, which preserves the snapshot binding.  The database file
path defaults to ``data/clm.db`` next to the project root and can be overridden
with the ``CLM_DB_PATH`` environment variable.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import ENGINE_VERSION
from .models import Pool, Position

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _PROJECT_ROOT / "data" / "clm.db"
_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    pool_id        TEXT PRIMARY KEY,
    token0         TEXT NOT NULL,
    token1         TEXT NOT NULL,
    fee_numerator  INTEGER NOT NULL,
    fee_denominator INTEGER NOT NULL,
    current_tick   INTEGER NOT NULL,
    positions_json TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quotes (
    quote_id       TEXT PRIMARY KEY,
    pool_id        TEXT NOT NULL,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    request_json   TEXT NOT NULL,
    result_json    TEXT NOT NULL,
    pool_digest    TEXT NOT NULL,
    quote_digest   TEXT NOT NULL,
    FOREIGN KEY (pool_id) REFERENCES pools(pool_id)
);
"""


def db_path() -> Path:
    return Path(os.environ.get("CLM_DB_PATH", str(_DEFAULT_DB)))


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

def insert_pool(pool_id: str, pool: Pool) -> None:
    positions = [[p.lower_tick, p.upper_tick, p.liquidity] for p in pool.positions]
    with _LOCK, get_conn() as conn:
        conn.execute(
            """INSERT INTO pools
               (pool_id, token0, token1, fee_numerator, fee_denominator,
                current_tick, positions_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                pool_id,
                pool.token0,
                pool.token1,
                pool.fee_numerator,
                pool.fee_denominator,
                pool.current_tick,
                json.dumps(positions, separators=(",", ":")),
            ),
        )


def get_pool(pool_id: str) -> Pool | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pools WHERE pool_id = ?", (pool_id,)).fetchone()
    if row is None:
        return None
    return _row_to_pool(row)


def list_pools(limit: int = 100) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT pool_id, token0, token1, fee_numerator, fee_denominator, "
            "current_tick, created_at FROM pools ORDER BY rowid DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _row_to_pool(row: sqlite3.Row) -> Pool:
    positions = tuple(
        Position(lower_tick=lo, upper_tick=hi, liquidity=lq)
        for lo, hi, lq in json.loads(row["positions_json"])
    )
    return Pool(
        pool_id=row["pool_id"],
        token0=row["token0"],
        token1=row["token1"],
        fee_numerator=row["fee_numerator"],
        fee_denominator=row["fee_denominator"],
        current_tick=row["current_tick"],
        positions=positions,
    )


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def insert_quote(
    quote_id: str,
    pool_id: str,
    request: dict[str, Any],
    result: dict[str, Any],
) -> None:
    with _LOCK, get_conn() as conn:
        conn.execute(
            """INSERT INTO quotes
               (quote_id, pool_id, request_json, result_json,
                pool_digest, quote_digest)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                quote_id,
                pool_id,
                json.dumps(request, sort_keys=True, separators=(",", ":")),
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                result["pool_digest"],
                result["quote_digest"],
            ),
        )


def get_quote(quote_id: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)).fetchone()
    if row is None:
        return None
    return {
        "quote_id": row["quote_id"],
        "pool_id": row["pool_id"],
        "created_at": row["created_at"],
        "request": json.loads(row["request_json"]),
        "result": json.loads(row["result_json"]),
        "pool_digest": row["pool_digest"],
        "quote_digest": row["quote_digest"],
        "engine_version": ENGINE_VERSION,
    }
