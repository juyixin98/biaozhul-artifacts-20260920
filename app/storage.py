# -*- coding: utf-8 -*-
"""SQLite 存储层。

设计要点：
- 每条模拟链拥有自己的全部行，按 chain_id 隔离；
- 开启 WAL + busy_timeout，支持多线程并发，终态写入靠事务 + 条件 UPDATE 互斥；
- SMT 节点存 smt_nodes，键值存 smt_kv，崩溃重启后用同一份持久数据恢复根；
- 每个线程使用独立连接（check_same_thread=False + threading.local）。
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS chains (
    chain_id        TEXT PRIMARY KEY,
    revision_number INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    time_nanos      INTEGER NOT NULL,
    privkey_hex     TEXT NOT NULL,
    pubkey_hex      TEXT NOT NULL,
    next_commit_seq INTEGER NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkpoints (
    chain_id   TEXT NOT NULL,
    height     INTEGER NOT NULL,
    time_nanos INTEGER NOT NULL,
    app_hash   TEXT NOT NULL,   -- hex(32)
    signature  TEXT NOT NULL,   -- hex(64) Ed25519
    seq        INTEGER NOT NULL,
    PRIMARY KEY (chain_id, height)
);

CREATE TABLE IF NOT EXISTS clients (
    client_id       TEXT PRIMARY KEY,
    local_chain_id  TEXT NOT NULL,   -- 该客户端登记在哪条链上
    remote_chain_id TEXT NOT NULL,   -- 它信任的对端链
    trusted_pubkey  TEXT NOT NULL,   -- 信任根：对端链 Ed25519 公钥
    trusting_nanos  INTEGER NOT NULL,
    last_height     INTEGER NOT NULL,
    last_time       INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS connections (
    conn_id        TEXT NOT NULL,
    chain_id       TEXT NOT NULL,
    client_id      TEXT NOT NULL,
    remote_conn_id TEXT NOT NULL,
    state          TEXT NOT NULL,
    PRIMARY KEY (conn_id, chain_id)
);

CREATE TABLE IF NOT EXISTS channels (
    channel_id       TEXT NOT NULL,
    chain_id         TEXT NOT NULL,
    port_id          TEXT NOT NULL DEFAULT 'transfer',
    conn_id          TEXT NOT NULL,
    state            TEXT NOT NULL,            -- INIT/TRYOPEN/OPEN/CLOSED
    ordering         TEXT NOT NULL,            -- ORDERED / UNORDERED
    version          TEXT NOT NULL,
    remote_channel   TEXT NOT NULL,
    remote_port      TEXT NOT NULL DEFAULT 'transfer',
    next_seq_send    INTEGER NOT NULL,
    next_seq_recv    INTEGER NOT NULL,
    next_seq_ack     INTEGER NOT NULL,
    counterparty_version TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (chain_id, channel_id)
);

CREATE TABLE IF NOT EXISTS packets (
    chain_id   TEXT NOT NULL,          -- 源链（承诺所在链）
    channel_id TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    dst_chain_id   TEXT NOT NULL,
    dst_channel_id TEXT NOT NULL,
    data       TEXT NOT NULL,          -- hex 负载
    timeout_revision_number INTEGER NOT NULL,
    timeout_height INTEGER NOT NULL,
    timeout_time   INTEGER NOT NULL,   -- 纳秒，0 表示不使用时间超时
    amount   INTEGER NOT NULL,
    sender   TEXT NOT NULL,
    receiver TEXT NOT NULL,
    status   TEXT NOT NULL,            -- IN_FLIGHT/DELIVERED/ACKED/TIMED_OUT
    PRIMARY KEY (chain_id, channel_id, sequence)
);

-- 目的链上的回执（无序通道；有序通道用 channels.next_seq_recv）
CREATE TABLE IF NOT EXISTS packet_receipts (
    chain_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    received   INTEGER NOT NULL,       -- 1
    PRIMARY KEY (chain_id, channel_id, sequence)
);

-- 源链上的确认（ack）存储
CREATE TABLE IF NOT EXISTS packet_acks (
    chain_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    sequence   INTEGER NOT NULL,
    ack        TEXT NOT NULL,          -- hex
    PRIMARY KEY (chain_id, channel_id, sequence)
);

-- 托管余额：(链, 通道, 账户)
CREATE TABLE IF NOT EXISTS escrow (
    chain_id   TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    account    TEXT NOT NULL,
    amount     INTEGER NOT NULL,
    PRIMARY KEY (chain_id, channel_id, account)
);

CREATE TABLE IF NOT EXISTS smt_nodes (
    chain_id TEXT NOT NULL,
    node_key BLOB NOT NULL,
    hash     BLOB NOT NULL,
    PRIMARY KEY (chain_id, node_key)
);

CREATE TABLE IF NOT EXISTS smt_kv (
    chain_id TEXT NOT NULL,
    key      BLOB NOT NULL,
    value    BLOB NOT NULL,
    PRIMARY KEY (chain_id, key)
);

CREATE INDEX IF NOT EXISTS idx_packets_dst
    ON packets(dst_chain_id, dst_channel_id, sequence);
CREATE INDEX IF NOT EXISTS idx_channels_lookup
    ON channels(chain_id, remote_channel);
"""


class SQLiteNodeStore:
    """供 SMT 使用的 NodeStore 实现，所有读写走外层事务连接。"""

    def __init__(self, conn: sqlite3.Connection, chain_id: str):
        self.conn = conn
        self.chain_id = chain_id

    def get_node(self, key: bytes) -> Optional[bytes]:
        row = self.conn.execute(
            "SELECT hash FROM smt_nodes WHERE chain_id=? AND node_key=?",
            (self.chain_id, key),
        ).fetchone()
        return row[0] if row else None

    def put_node(self, key: bytes, value_hash: bytes) -> None:
        self.conn.execute(
            "INSERT INTO smt_nodes(chain_id, node_key, hash) VALUES(?,?,?) "
            "ON CONFLICT(chain_id, node_key) DO UPDATE SET hash=excluded.hash",
            (self.chain_id, key, value_hash),
        )

    def delete_node(self, key: bytes) -> None:
        self.conn.execute(
            "DELETE FROM smt_nodes WHERE chain_id=? AND node_key=?",
            (self.chain_id, key),
        )

    def get_value(self, key: bytes) -> Optional[bytes]:
        row = self.conn.execute(
            "SELECT value FROM smt_kv WHERE chain_id=? AND key=?",
            (self.chain_id, key),
        ).fetchone()
        return row[0] if row else None

    def put_value(self, key: bytes, value: bytes) -> None:
        self.conn.execute(
            "INSERT INTO smt_kv(chain_id, key, value) VALUES(?,?,?) "
            "ON CONFLICT(chain_id, key) DO UPDATE SET value=excluded.value",
            (self.chain_id, key, value),
        )

    def delete_value(self, key: bytes) -> None:
        self.conn.execute(
            "DELETE FROM smt_kv WHERE chain_id=? AND key=?",
            (self.chain_id, key),
        )


class Database:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                self.path,
                timeout=30,
                isolation_level=None,  # 手动 BEGIN/COMMIT
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self.connect()
        conn.executescript(SCHEMA)

    # 便利查询
    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, tuple(params)).fetchall()

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> Optional[sqlite3.Row]:
        return self.connect().execute(sql, tuple(params)).fetchone()
