"""SQLite 存储层。

只追加、不可变：池快照创建后永不更新；每次报价插入一行报价与若干逐段
证据行。引擎不写任何“储备”——池没有可被修改的余额状态。
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import Optional

DB_PATH = os.environ.get("CLMM_DB_PATH", os.path.join(os.getcwd(), "clmm.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pools (
    pool_id          TEXT PRIMARY KEY,
    created_at       TEXT NOT NULL,
    token0           TEXT NOT NULL,
    token1           TEXT NOT NULL,
    fee_ppm          INTEGER NOT NULL,
    sqrt_price_x96   TEXT NOT NULL,
    snapshot_hash    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    pool_id     TEXT NOT NULL,
    idx         INTEGER NOT NULL,
    lower_tick  INTEGER NOT NULL,
    upper_tick  INTEGER NOT NULL,
    liquidity   TEXT NOT NULL,
    PRIMARY KEY (pool_id, idx),
    FOREIGN KEY (pool_id) REFERENCES pools(pool_id)
);
CREATE TABLE IF NOT EXISTS quotes (
    quote_id        TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    pool_id         TEXT NOT NULL,
    snapshot_hash   TEXT NOT NULL,
    zero_for_one    INTEGER NOT NULL,
    amount_in       TEXT NOT NULL,
    limit_tick      INTEGER,
    stop_reason     TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    signature       TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quote_segments (
    quote_id      TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    tick_lo       INTEGER NOT NULL,
    tick_hi       INTEGER NOT NULL,
    liquidity     TEXT NOT NULL,
    fee           TEXT NOT NULL,
    principal_in  TEXT NOT NULL,
    amount_out    TEXT NOT NULL,
    ended_by      TEXT NOT NULL,
    crossed_tick  INTEGER,
    PRIMARY KEY (quote_id, segment_index),
    FOREIGN KEY (quote_id) REFERENCES quotes(quote_id)
);
"""


def connect(db_path: Optional[str] = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    # check_same_thread=False：FastAPI 的同步端点在线程池执行，
    # 连接在主线程（lifespan）创建、请求线程使用。SQLite 连接对象本身
    # 不被多线程并发共享（默认线程池 + 单次请求串行使用）。
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(_SCHEMA)
    return conn


def insert_pool(conn: sqlite3.Connection, snapshot: dict) -> None:
    conn.execute(
        """INSERT INTO pools
           (pool_id, created_at, token0, token1, fee_ppm, sqrt_price_x96, snapshot_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            snapshot["pool_id"],
            snapshot["created_at"],
            snapshot["token0"],
            snapshot["token1"],
            int(snapshot["fee_ppm"]),
            str(snapshot["sqrt_price_x96"]),
            snapshot["snapshot_hash"],
        ),
    )
    for i, p in enumerate(snapshot["positions"]):
        conn.execute(
            """INSERT INTO positions (pool_id, idx, lower_tick, upper_tick, liquidity)
               VALUES (?, ?, ?, ?, ?)""",
            (snapshot["pool_id"], i, p["lower_tick"], p["upper_tick"], str(p["liquidity"])),
        )
    conn.commit()


def get_pool_row(conn: sqlite3.Connection, pool_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM pools WHERE pool_id = ?", (pool_id,)).fetchone()


def list_pool_ids(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT pool_id FROM pools ORDER BY pool_id")]


def get_positions(conn: sqlite3.Connection, pool_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT lower_tick, upper_tick, liquidity FROM positions WHERE pool_id = ? ORDER BY idx",
        (pool_id,),
    ).fetchall()


def insert_quote(conn: sqlite3.Connection, payload: dict, signature: str, request_hash: str) -> None:
    r = payload["result"]
    conn.execute(
        """INSERT INTO quotes
           (quote_id, created_at, pool_id, snapshot_hash, zero_for_one,
            amount_in, limit_tick, stop_reason, request_hash, signature, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            payload["quote_id"],
            payload["created_at"],
            payload["pool_id"],
            payload["snapshot_hash"],
            1 if payload["zero_for_one"] else 0,
            payload["amount_in"],
            payload.get("limit_tick"),
            r["stop_reason"],
            request_hash,
            signature,
            json.dumps(payload, ensure_ascii=False),
        ),
    )
    for seg in r["segments"]:
        conn.execute(
            """INSERT INTO quote_segments
               (quote_id, segment_index, tick_lo, tick_hi, liquidity, fee,
                principal_in, amount_out, ended_by, crossed_tick)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["quote_id"],
                seg["segment_index"],
                seg["tick_lo"],
                seg["tick_hi"],
                seg["liquidity"],
                seg["fee"],
                seg["principal_in"],
                seg["amount_out"],
                seg["ended_by"],
                seg["crossed_tick"],
            ),
        )
    conn.commit()


def get_quote_row(conn: sqlite3.Connection, quote_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM quotes WHERE quote_id = ?", (quote_id,)).fetchone()
