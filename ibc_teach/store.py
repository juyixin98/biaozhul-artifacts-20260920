"""SQLite 状态存储与 IBC 教学状态机（真实执行）。

两条链（chain-a / chain-b）同处一个 SQLite 文件，**这是明确的教学简化**：
真实 IBC 中两条链各自独立维护状态库，由外部中继者搬运消息。本模型把中继者
逻辑放进同一个进程的 API 里，便于观察和测试。

终态互斥（ack 与 timeout-refund 只能落一种）在 SQL 层保证：
``UPDATE packets SET status=? WHERE id=? AND status='SENT'`` 为条件更新，
配合 ``BEGIN IMMEDIATE`` 写事务串行化，并发下恰有一个事务能把包带出 SENT。

参考语义（简化）：
* 包承诺：在源链生成 ``commitment(packet)``（ICS-004 SendPacket）；
* 接收：目的链按序/无序处理，写入回执/接收记录（RecvPacket）；
* 确认：源链凭目的链已处理证明删除承诺、释放托管（Acknowledgement）；
* 超时退款：源链在超时高度/时间已到达且目的链证明“未处理”后退款（Timeout）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from . import crypto

GENESIS_HEIGHT = 0
GENESIS_PREV_HASH = "00" * 32

# ---------- 错误类型 ----------


class IBCError(Exception):
    """所有可预期协议错误的基类。"""

    http_status = 400
    code = "ibc_error"

    def __init__(self, message: str = "", *, code: Optional[str] = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ErrNotFound(IBCError):
    http_status = 404
    code = "not_found"


class ErrClosed(IBCError):
    http_status = 409
    code = "channel_closed"


class ErrTimeoutHeight(IBCError):
    http_status = 408
    code = "timeout_height_reached"


class ErrTimeoutTimestamp(IBCError):
    http_status = 408
    code = "timeout_timestamp_reached"


class ErrSeqGap(IBCError):
    http_status = 409
    code = "sequence_gap"


class ErrAlreadyExists(IBCError):
    http_status = 409
    code = "already_exists"


class ErrTerminal(IBCError):
    """包已处于终态（ACKED / TIMED_OUT），不能再次转移。"""

    http_status = 409
    code = "terminal_state"


class ErrProof(IBCError):
    """证明或检查点验证失败：缺失、签名错误、高度陈旧、伪造成员关系等。"""

    http_status = 400
    code = "proof_verification_failed"


class ErrBadRequest(IBCError):
    http_status = 400
    code = "bad_request"


class ErrIntegrity(IBCError):
    """启动时块链/状态根完整性校验失败（模拟崩溃后检测到脏库）。"""

    http_status = 500
    code = "integrity_check_failed"


# ---------- 键路径 / 编码 ----------


def _u64be(n: int) -> bytes:
    if not (0 <= n < 2**63):
        raise ErrBadRequest("sequence out of range")
    return n.to_bytes(8, "big")


def commitment_key(port: str, channel: str, sequence: int) -> bytes:
    """源链： packets/commitments/{port}/{channel}/{seq}"""
    return f"packets/commitments/{port}/{channel}/".encode() + _u64be(sequence)


def receipt_key(port: str, channel: str, sequence: int) -> bytes:
    """目的链：packets/receipts/{port}/{channel}/{seq}（无序通道使用）。"""
    return f"packets/receipts/{port}/{channel}/".encode() + _u64be(sequence)


def next_seq_recv_key(port: str, channel: str) -> bytes:
    return f"channels/nextSequenceRecv/{port}/{channel}".encode()


def next_seq_ack_key(port: str, channel: str) -> bytes:
    return f"channels/nextSequenceAck/{port}/{channel}".encode()


def next_seq_send_key(port: str, channel: str) -> bytes:
    return f"channels/nextSequenceSend/{port}/{channel}".encode()


def channel_key(port: str, channel: str) -> bytes:
    return f"channels/channel/{port}/{channel}".encode()


def packet_commitment_value(
    *,
    timeout_height: int,
    timeout_time_ns: int,
    data_hex: str,
    amount: int,
) -> bytes:
    """包承诺体：规范化 JSON（与 ICS-004 的 sha256(timeout||data) 同构，字段更多）。"""
    payload = {
        "timeout_height": int(timeout_height),
        "timeout_time_ns": int(timeout_time_ns),
        "data_hex": data_hex,
        "amount": int(amount),
    }
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")


# ---------- 数据对象 ----------


@dataclass
class Channel:
    chain_id: str
    port_id: str
    channel_id: str
    counterparty_chain: str
    counterparty_port: str
    counterparty_channel: str
    ordering: str  # "ordered" | "unordered"
    version: str
    next_seq_send: int
    next_seq_recv: int
    next_seq_ack: int
    closed: bool


@dataclass
class Packet:
    id: int
    src_chain: str
    src_port: str
    src_channel: str
    dst_chain: str
    dst_port: str
    dst_channel: str
    sequence: int
    timeout_height: int  # 目的链绝对高度；0 表示不设高度超时
    timeout_time_ns: int  # 目的链绝对纳秒时间；0 表示不设时间超时
    data_hex: str
    amount: int
    status: str  # SENT / RECEIVED / ACKED / TIMED_OUT
    delivered: bool
    delivered_at_height: Optional[int]


SCHEMA = """
CREATE TABLE IF NOT EXISTS chains (
    chain_id        TEXT PRIMARY KEY,
    verify_key_hex  TEXT NOT NULL,
    signing_key_hex TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    chain_id          TEXT NOT NULL,
    height            INTEGER NOT NULL,
    time_ns           INTEGER NOT NULL,
    app_hash          TEXT NOT NULL,
    previous_app_hash TEXT NOT NULL,
    signature_hex     TEXT NOT NULL,
    PRIMARY KEY (chain_id, height)
);
CREATE TABLE IF NOT EXISTS kv_snapshots (
    chain_id TEXT NOT NULL,
    height   INTEGER NOT NULL,
    kv_json  TEXT NOT NULL,
    PRIMARY KEY (chain_id, height)
);
CREATE TABLE IF NOT EXISTS channels (
    chain_id            TEXT NOT NULL,
    port_id             TEXT NOT NULL,
    channel_id          TEXT NOT NULL,
    counterparty_chain  TEXT NOT NULL,
    counterparty_port   TEXT NOT NULL,
    counterparty_channel TEXT NOT NULL,
    ordering            TEXT NOT NULL CHECK (ordering IN ('ordered','unordered')),
    version             TEXT NOT NULL,
    next_seq_send       INTEGER NOT NULL DEFAULT 1,
    next_seq_recv       INTEGER NOT NULL DEFAULT 1,
    next_seq_ack        INTEGER NOT NULL DEFAULT 1,
    closed              INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chain_id, port_id, channel_id)
);
CREATE TABLE IF NOT EXISTS packets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    src_chain   TEXT NOT NULL,
    src_port    TEXT NOT NULL,
    src_channel TEXT NOT NULL,
    dst_chain   TEXT NOT NULL,
    dst_port    TEXT NOT NULL,
    dst_channel TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    timeout_height   INTEGER NOT NULL,
    timeout_time_ns  INTEGER NOT NULL,
    data_hex    TEXT NOT NULL,
    amount      INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'SENT'
        CHECK (status IN ('SENT','RECEIVED','ACKED','TIMED_OUT')),
    delivered   INTEGER NOT NULL DEFAULT 0,
    delivered_at_height INTEGER,
    UNIQUE (src_chain, src_port, src_channel, sequence)
);
CREATE TABLE IF NOT EXISTS clients (
    chain_id             TEXT NOT NULL,           -- 持有客户端的链
    cp_chain_id          TEXT NOT NULL,           -- 被信任的对端链
    cp_port_id           TEXT NOT NULL,
    cp_channel_id        TEXT NOT NULL,
    trusted_height       INTEGER NOT NULL,
    PRIMARY KEY (chain_id, cp_chain_id, cp_port_id, cp_channel_id)
);
"""


class Store:
    """线程安全的 SQLite 状态库封装。每个操作使用独立连接 + 即时事务。"""

    def __init__(self, path: str = "ibc_teach.db") -> None:
        self.path = path
        self._lock = threading.RLock()
        first = self._connect()
        try:
            with first:
                first.executescript(SCHEMA)
                first.execute("PRAGMA journal_mode=WAL")
                first.execute("PRAGMA synchronous=FULL")
            self._bootstrap(first)
        finally:
            first.close()

    # ---- 连接 / 底层 ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _txn(self, conn: sqlite3.Connection) -> None:
        # 应对并发：SQLITE_BUSY 时短暂重试。
        for attempt in range(100):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as e:
                if "locked" in str(e) or "busy" in str(e):
                    time.sleep(0.01)
                    continue
                raise
        raise IBCError("database lock contention", code="db_locked")

    @staticmethod
    def _finish(conn: sqlite3.Connection) -> None:
        """写事务收尾：成功提交、异常回滚（连接随后关闭）。

        对只读连接（未开启事务）调用也是安全的：commit/rollback 均为空操作。
        """
        import sys

        if sys.exc_info()[0] is None:
            conn.commit()
        else:
            conn.rollback()
        conn.close()

    def _bootstrap(self, conn: sqlite3.Connection) -> None:
        row = conn.execute("SELECT COUNT(*) AS n FROM chains").fetchone()
        if row["n"] > 0:
            return
        # 创世：为两条链生成真实 Ed25519 密钥与高度 0 的签名检查点。
        gen_time = time.time_ns()
        empty_root = crypto.SparseMerkleTree().root().hex()
        for chain_id in ("chain-a", "chain-b"):
            vk, sk = crypto.generate_signing_key()
            sig = crypto.sign_checkpoint(
                sk, chain_id, GENESIS_HEIGHT, gen_time, empty_root, GENESIS_PREV_HASH
            )
            conn.execute(
                "INSERT INTO chains(chain_id, verify_key_hex, signing_key_hex) VALUES (?,?,?)",
                (chain_id, vk, sk),
            )
            conn.execute(
                "INSERT INTO blocks(chain_id,height,time_ns,app_hash,"
                "previous_app_hash,signature_hex) VALUES (?,?,?,?,?,?)",
                (chain_id, GENESIS_HEIGHT, gen_time, empty_root, GENESIS_PREV_HASH, sig),
            )
            conn.execute(
                "INSERT INTO kv_snapshots(chain_id,height,kv_json) VALUES (?,?,?)",
                (chain_id, GENESIS_HEIGHT, "{}"),
            )

    # ---- 链 / 检查点读取 ----

    def list_chains(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT chain_id, verify_key_hex FROM chains ORDER BY chain_id"
            ).fetchall()
            out = []
            for r in rows:
                tip = conn.execute(
                    "SELECT height, time_ns, app_hash FROM blocks "
                    "WHERE chain_id=? ORDER BY height DESC LIMIT 1",
                    (r["chain_id"],),
                ).fetchone()
                out.append(
                    {
                        "chain_id": r["chain_id"],
                        "verify_key": r["verify_key_hex"],
                        "tip_height": tip["height"],
                        "tip_time_ns": tip["time_ns"],
                        "app_hash": tip["app_hash"],
                    }
                )
            return out
        finally:
            self._finish(conn)

    def _signing_key(self, conn: sqlite3.Connection, chain_id: str) -> str:
        row = conn.execute(
            "SELECT signing_key_hex FROM chains WHERE chain_id=?", (chain_id,)
        ).fetchone()
        if row is None:
            raise ErrNotFound(f"unknown chain {chain_id}")
        return row["signing_key_hex"]

    def verify_key(self, chain_id: str) -> str:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT verify_key_hex FROM chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
            if row is None:
                raise ErrNotFound(f"unknown chain {chain_id}")
            return row["verify_key_hex"]
        finally:
            self._finish(conn)

    def tip(self, chain_id: str) -> sqlite3.Row:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM blocks WHERE chain_id=? ORDER BY height DESC LIMIT 1",
                (chain_id,),
            ).fetchone()
            if row is None:
                raise ErrNotFound(f"unknown chain {chain_id}")
            return row
        finally:
            self._finish(conn)

    def block(self, chain_id: str, height: int) -> sqlite3.Row:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM blocks WHERE chain_id=? AND height=?", (chain_id, height)
            ).fetchone()
            if row is None:
                raise ErrNotFound(f"block {chain_id}@{height} not found")
            return row
        finally:
            self._finish(conn)

    # ---- 实时状态 -> KV（用于状态根与证明） ----

    def _live_kv(self, conn: sqlite3.Connection, chain_id: str) -> dict[bytes, bytes]:
        kv: dict[bytes, bytes] = {}
        for ch in conn.execute(
            "SELECT * FROM channels WHERE chain_id=?", (chain_id,)
        ).fetchall():
            if ch["closed"]:
                continue  # 关闭后通道键从承诺空间移除（简化处理）
            p, c = ch["port_id"], ch["channel_id"]
            binding = json.dumps(
                {
                    "ordering": ch["ordering"],
                    "version": ch["version"],
                    "counterparty_chain": ch["counterparty_chain"],
                    "counterparty_port": ch["counterparty_port"],
                    "counterparty_channel": ch["counterparty_channel"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            kv[channel_key(p, c)] = binding
            kv[next_seq_send_key(p, c)] = _u64be(ch["next_seq_send"])
            if ch["ordering"] == "ordered":
                kv[next_seq_recv_key(p, c)] = _u64be(ch["next_seq_recv"])
                kv[next_seq_ack_key(p, c)] = _u64be(ch["next_seq_ack"])

        for pk in conn.execute(
            "SELECT * FROM packets WHERE src_chain=? AND status IN ('SENT','RECEIVED')",
            (chain_id,),
        ).fetchall():
            kv[commitment_key(pk["src_port"], pk["src_channel"], pk["sequence"])] = (
                packet_commitment_value(
                    timeout_height=pk["timeout_height"],
                    timeout_time_ns=pk["timeout_time_ns"],
                    data_hex=pk["data_hex"],
                    amount=pk["amount"],
                )
            )

        for pk in conn.execute(
            "SELECT * FROM packets WHERE dst_chain=? AND delivered=1", (chain_id,)
        ).fetchall():
            p, c = pk["dst_port"], pk["dst_channel"]
            # 查该目的通道是否有序，决定写哪种接收证据键。
            ch = conn.execute(
                "SELECT ordering FROM channels WHERE chain_id=? AND port_id=? "
                "AND channel_id=?",
                (chain_id, p, c),
            ).fetchone()
            if ch is not None and ch["ordering"] == "unordered":
                # 无序：逐包回执（值为固定 0x01，与 ICS-004 receipt 占位同构）。
                kv[receipt_key(p, c, pk["sequence"])] = b"\x01"
            # ordered 通道只由 nextSequenceRecv 单一键表达“< seq 均已收”。

        return kv

    def _snapshot_kv(self, chain_id: str, height: int) -> dict[bytes, bytes]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT kv_json FROM kv_snapshots WHERE chain_id=? AND height=?",
                (chain_id, height),
            ).fetchone()
            if row is None:
                raise ErrNotFound(f"snapshot {chain_id}@{height} not found")
            raw = json.loads(row["kv_json"])
            return {bytes.fromhex(k): bytes.fromhex(v) for k, v in raw.items()}
        finally:
            self._finish(conn)

    # ---- 出块（生成签名检查点） ----

    def finalize_block(
        self,
        chain_id: str,
        time_ns: Optional[int] = None,
        height: Optional[int] = None,
    ) -> dict[str, Any]:
        """执行一个块：提交当前实时状态、计算 SMT 根、用共识密钥签名。

        高度必须连续递增；时间必须单调递增（真实链的要求，防重放/回拨）。
        """
        conn = self._connect()
        try:
            self._txn(conn)
            tip = conn.execute(
                "SELECT * FROM blocks WHERE chain_id=? ORDER BY height DESC LIMIT 1",
                (chain_id,),
            ).fetchone()
            if tip is None:
                raise ErrNotFound(f"unknown chain {chain_id}")
            new_height = tip["height"] + 1 if height is None else height
            if new_height != tip["height"] + 1:
                raise ErrBadRequest(
                    f"non-contiguous height: tip={tip['height']} got={new_height}"
                )
            now_ns = time.time_ns() if time_ns is None else time_ns
            if now_ns <= tip["time_ns"]:
                raise ErrBadRequest("block time must be strictly increasing")

            kv = self._live_kv(conn, chain_id)
            app_hash = crypto.SparseMerkleTree(kv).root().hex()
            sk = self._signing_key(conn, chain_id)
            sig = crypto.sign_checkpoint(
                sk, chain_id, new_height, now_ns, app_hash, tip["app_hash"]
            )
            conn.execute(
                "INSERT INTO blocks(chain_id,height,time_ns,app_hash,"
                "previous_app_hash,signature_hex) VALUES (?,?,?,?,?,?)",
                (chain_id, new_height, now_ns, app_hash, tip["app_hash"], sig),
            )
            snap = {k.hex(): v.hex() for k, v in kv.items()}
            conn.execute(
                "INSERT INTO kv_snapshots(chain_id,height,kv_json) VALUES (?,?,?)",
                (chain_id, new_height, json.dumps(snap, sort_keys=True)),
            )
            return {
                "chain_id": chain_id,
                "height": new_height,
                "time_ns": now_ns,
                "app_hash": app_hash,
                "previous_app_hash": tip["app_hash"],
                "signature": sig,
                "verify_key": self.verify_key(chain_id),
            }
        finally:
            self._finish(conn)

    # ---- 通道管理 ----

    def create_channel(
        self,
        *,
        ordering: str,
        version: str,
        port_a: str = "port-a",
        port_b: str = "port-b",
        channel_a: str = "channel-0",
        channel_b: str = "channel-0",
    ) -> dict[str, Any]:
        """在两条链上成对创建互相绑定的通道端（绑定顺序模式/对端/版本）。"""
        if ordering not in ("ordered", "unordered"):
            raise ErrBadRequest("ordering must be 'ordered' or 'unordered'")
        if not version:
            raise ErrBadRequest("version required")
        conn = self._connect()
        try:
            self._txn(conn)
            exists = conn.execute(
                "SELECT 1 FROM channels WHERE chain_id='chain-a' AND port_id=? "
                "AND channel_id=?",
                (port_a, channel_a),
            ).fetchone()
            if exists:
                raise ErrAlreadyExists("channel already exists")
            rows = [
                ("chain-a", port_a, channel_a, "chain-b", port_b, channel_b),
                ("chain-b", port_b, channel_b, "chain-a", port_a, channel_a),
            ]
            for chain_id, p, c, cp_chain, cp_port, cp_chan in rows:
                conn.execute(
                    "INSERT INTO channels(chain_id,port_id,channel_id,"
                    "counterparty_chain,counterparty_port,counterparty_channel,"
                    "ordering,version) VALUES (?,?,?,?,?,?,?,?)",
                    (chain_id, p, c, cp_chain, cp_port, cp_chan, ordering, version),
                )
            return {"ordering": ordering, "version": version, "ends": rows}
        finally:
            self._finish(conn)

    def _get_channel_conn(
        self, conn: sqlite3.Connection, chain_id: str, port: str, channel: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM channels WHERE chain_id=? AND port_id=? AND channel_id=?",
            (chain_id, port, channel),
        ).fetchone()
        if row is None:
            raise ErrNotFound(f"channel {port}/{channel} on {chain_id} not found")
        return row

    def get_channel(self, chain_id: str, port: str, channel: str) -> Channel:
        conn = self._connect()
        try:
            r = self._get_channel_conn(conn, chain_id, port, channel)
            return Channel(
                chain_id=r["chain_id"],
                port_id=r["port_id"],
                channel_id=r["channel_id"],
                counterparty_chain=r["counterparty_chain"],
                counterparty_port=r["counterparty_port"],
                counterparty_channel=r["counterparty_channel"],
                ordering=r["ordering"],
                version=r["version"],
                next_seq_send=r["next_seq_send"],
                next_seq_recv=r["next_seq_recv"],
                next_seq_ack=r["next_seq_ack"],
                closed=bool(r["closed"]),
            )
        finally:
            self._finish(conn)

    def list_channels(self, chain_id: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT port_id, channel_id, counterparty_chain, counterparty_port, "
                "counterparty_channel, ordering, version, next_seq_send, next_seq_recv, "
                "next_seq_ack, closed FROM channels WHERE chain_id=? ORDER BY port_id, channel_id",
                (chain_id,),
            ).fetchall()]
        finally:
            self._finish(conn)

    def close_channel(self, chain_id: str, port: str, channel: str) -> dict[str, Any]:
        conn = self._connect()
        try:
            self._txn(conn)
            ch = self._get_channel_conn(conn, chain_id, port, channel)
            if ch["closed"]:
                raise ErrClosed("channel already closed")
            conn.execute(
                "UPDATE channels SET closed=1 WHERE chain_id=? AND port_id=? "
                "AND channel_id=?",
                (chain_id, port, channel),
            )
            return {"chain_id": chain_id, "port_id": port, "channel_id": channel, "closed": True}
        finally:
            self._finish(conn)

    # ---- 证明查询（中继者使用） ----

    def proof(self, chain_id: str, height: int, key: bytes) -> dict[str, Any]:
        """返回指定高度快照下某键的成员/非成员证明 + 该块签名检查点。"""
        block = self.block(chain_id, height)
        kv = self._snapshot_kv(chain_id, height)
        value = kv.get(key)
        tree = crypto.SparseMerkleTree(kv)
        steps = [s.to_dict() for s in tree.prove(key)]
        return {
            "checkpoint": {
                "chain_id": chain_id,
                "height": block["height"],
                "time_ns": block["time_ns"],
                "app_hash": block["app_hash"],
                "previous_app_hash": block["previous_app_hash"],
                "signature": block["signature_hex"],
                "verify_key": self.verify_key(chain_id),
            },
            "path": key.hex(),
            "value_hex": value.hex() if value is not None else None,
            "exists": value is not None,
            "proof": {"steps": steps},
        }

    # ---- 对端检查点 / 证明验证（信任根） ----

    def _verify_checkpoint_and_update_client(
        self,
        conn: sqlite3.Connection,
        *,
        local_chain: str,
        cp: dict[str, Any],
        local_cp_port: str,
        local_cp_channel: str,
    ) -> sqlite3.Row:
        """校验对端检查点并更新轻客户端信任高度；任何缺陷 -> ErrProof。"""
        required = ("chain_id", "height", "time_ns", "app_hash", "signature", "verify_key")
        if any(cp.get(k) is None for k in required):
            raise ErrProof("checkpoint missing required fields")
        cp_chain = cp["chain_id"]
        height = int(cp["height"])

        # 1) 验证密钥必须与对端链创世登记的共识密钥一致（信任根，拒绝错钥/旧钥）。
        row = conn.execute(
            "SELECT verify_key_hex FROM chains WHERE chain_id=?", (cp_chain,)
        ).fetchone()
        if row is None:
            raise ErrProof(f"unknown counterparty chain {cp_chain!r}")
        if cp["verify_key"] != row["verify_key_hex"]:
            raise ErrProof("checkpoint verify key does not match trusted consensus key")

        # 2) 签名必须真实覆盖 height/time/root/prev_root。
        ok = crypto.verify_checkpoint_signature(
            row["verify_key_hex"],
            cp_chain,
            height,
            int(cp["time_ns"]),
            cp["app_hash"],
            cp.get("previous_app_hash", ""),
            cp["signature"],
        )
        if not ok:
            raise ErrProof("checkpoint signature invalid")

        # 3) 检查点必须真实存在且字段与存档一致（防止拿真签名拼改其他字段重放）。
        stored = conn.execute(
            "SELECT * FROM blocks WHERE chain_id=? AND height=?", (cp_chain, height)
        ).fetchone()
        if stored is None:
            raise ErrProof("checkpoint height unknown")
        if (
            stored["app_hash"] != cp["app_hash"]
            or stored["time_ns"] != int(cp["time_ns"])
            or stored["previous_app_hash"] != cp.get("previous_app_hash")
        ):
            raise ErrProof("checkpoint fields do not match stored block")

        # 4) 轻客户端：该通道绑定的对端高度不得回退（拒绝旧检查点重放）。
        cl = conn.execute(
            "SELECT trusted_height FROM clients WHERE chain_id=? AND cp_chain_id=? "
            "AND cp_port_id=? AND cp_channel_id=?",
            (local_chain, cp_chain, local_cp_port, local_cp_channel),
        ).fetchone()
        if cl is not None and height < cl["trusted_height"]:
            raise ErrProof(
                f"stale checkpoint: trusted={cl['trusted_height']} got={height}"
            )
        conn.execute(
            "INSERT INTO clients(chain_id,cp_chain_id,cp_port_id,cp_channel_id,"
            "trusted_height) VALUES (?,?,?,?,?) "
            "ON CONFLICT(chain_id,cp_chain_id,cp_port_id,cp_channel_id) "
            "DO UPDATE SET trusted_height=excluded.trusted_height",
            (local_chain, cp_chain, local_cp_port, local_cp_channel, height),
        )
        return stored

    @staticmethod
    def _check_proof(
        cp: dict[str, Any], key: bytes, value: Optional[bytes], proof: dict[str, Any]
    ) -> None:
        try:
            steps = [crypto.ProofStep.from_dict(s) for s in proof["steps"]]
        except (KeyError, TypeError, ValueError):
            raise ErrProof("malformed proof steps")
        if not crypto.verify_proof(cp["app_hash"], key, value, steps):
            raise ErrProof("merle proof does not match checkpoint app hash")

    # ---- 发送 ----

    def send_packet(
        self,
        *,
        src_chain: str,
        src_port: str,
        src_channel: str,
        timeout_height: int,
        timeout_time_ns: int,
        data_hex: str,
        amount: int = 0,
    ) -> dict[str, Any]:
        if timeout_height < 0 or timeout_time_ns < 0:
            raise ErrBadRequest("timeout values must be >= 0")
        try:
            bytes.fromhex(data_hex)
        except ValueError:
            raise ErrBadRequest("data_hex is not valid hex")
        conn = self._connect()
        try:
            self._txn(conn)
            ch = self._get_channel_conn(conn, src_chain, src_port, src_channel)
            if ch["closed"]:
                raise ErrClosed("source channel is closed")
            seq = ch["next_seq_send"]
            conn.execute(
                "UPDATE channels SET next_seq_send=? WHERE chain_id=? AND port_id=? "
                "AND channel_id=?",
                (seq + 1, src_chain, src_port, src_channel),
            )
            conn.execute(
                "INSERT INTO packets(src_chain,src_port,src_channel,dst_chain,"
                "dst_port,dst_channel,sequence,timeout_height,timeout_time_ns,"
                "data_hex,amount,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,'SENT')",
                (
                    src_chain, src_port, src_channel,
                    ch["counterparty_chain"], ch["counterparty_port"],
                    ch["counterparty_channel"], seq,
                    int(timeout_height), int(timeout_time_ns),
                    data_hex, int(amount),
                ),
            )
            return {
                "sequence": seq,
                "src_chain": src_chain,
                "src_port": src_port,
                "src_channel": src_channel,
                "dst_chain": ch["counterparty_chain"],
                "dst_port": ch["counterparty_port"],
                "dst_channel": ch["counterparty_channel"],
                "timeout_height": int(timeout_height),
                "timeout_time_ns": int(timeout_time_ns),
                "data_hex": data_hex,
                "amount": int(amount),
                "status": "SENT",
            }
        finally:
            self._finish(conn)

    def get_packet(self, src_chain: str, src_port: str, src_channel: str, sequence: int) -> Packet:
        conn = self._connect()
        try:
            r = conn.execute(
                "SELECT * FROM packets WHERE src_chain=? AND src_port=? "
                "AND src_channel=? AND sequence=?",
                (src_chain, src_port, src_channel, sequence),
            ).fetchone()
            if r is None:
                raise ErrNotFound("packet not found")
            return self._row_to_packet(r)
        finally:
            self._finish(conn)

    @staticmethod
    def _row_to_packet(r: sqlite3.Row) -> Packet:
        return Packet(
            id=r["id"],
            src_chain=r["src_chain"], src_port=r["src_port"], src_channel=r["src_channel"],
            dst_chain=r["dst_chain"], dst_port=r["dst_port"], dst_channel=r["dst_channel"],
            sequence=r["sequence"], timeout_height=r["timeout_height"],
            timeout_time_ns=r["timeout_time_ns"], data_hex=r["data_hex"],
            amount=r["amount"], status=r["status"], delivered=bool(r["delivered"]),
            delivered_at_height=r["delivered_at_height"],
        )

    def list_packets(self, chain_id: Optional[str] = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if chain_id:
                rows = conn.execute(
                    "SELECT * FROM packets WHERE src_chain=? OR dst_chain=? "
                    "ORDER BY id",
                    (chain_id, chain_id),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM packets ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            self._finish(conn)

    # ---- 接收（RecvPacket） ----

    def recv_packet(self, packet: dict[str, Any], cp: dict[str, Any], proof: dict[str, Any]) -> dict[str, Any]:
        conn = self._connect()
        try:
            self._txn(conn)
            # 本地目的通道绑定检查（顺序模式、对端、版本必须与包携带的信息一致）。
            ch = self._get_channel_conn(
                conn, packet["dst_chain"], packet["dst_port"], packet["dst_channel"]
            )
            if ch["closed"]:
                raise ErrClosed("destination channel is closed")
            if ch["counterparty_chain"] != packet["src_chain"] or \
               ch["counterparty_port"] != packet["src_port"] or \
               ch["counterparty_channel"] != packet["src_channel"]:
                raise ErrProof("packet counterparty does not match local channel binding")
            if cp.get("chain_id") != packet["src_chain"]:
                raise ErrProof("checkpoint chain_id does not match packet source")

            # 信任对端检查点（含旧检查点拒绝），并验证包承诺成员证明。
            self._verify_checkpoint_and_update_client(
                conn,
                local_chain=packet["dst_chain"],
                cp=cp,
                local_cp_port=ch["counterparty_port"],
                local_cp_channel=ch["counterparty_channel"],
            )
            key = commitment_key(packet["src_port"], packet["src_channel"], int(packet["sequence"]))
            value = packet_commitment_value(
                timeout_height=int(packet["timeout_height"]),
                timeout_time_ns=int(packet["timeout_time_ns"]),
                data_hex=packet["data_hex"],
                amount=int(packet.get("amount", 0)),
            )
            self._check_proof(cp, key, value, proof)

            # 超时判定：以本链最新终态（已出块）高度/时间为准。
            tip = conn.execute(
                "SELECT height,time_ns FROM blocks WHERE chain_id=? "
                "ORDER BY height DESC LIMIT 1",
                (packet["dst_chain"],),
            ).fetchone()
            th, tt = int(packet["timeout_height"]), int(packet["timeout_time_ns"])
            if th and tip["height"] >= th:
                raise ErrTimeoutHeight(f"timeout height {th} reached at {tip['height']}")
            if tt and tip["time_ns"] >= tt:
                raise ErrTimeoutTimestamp("packet timeout timestamp already elapsed")

            existing = conn.execute(
                "SELECT * FROM packets WHERE src_chain=? AND src_port=? "
                "AND src_channel=? AND sequence=?",
                (packet["src_chain"], packet["src_port"], packet["src_channel"],
                 int(packet["sequence"])),
            ).fetchone()
            if existing is None:
                raise ErrNotFound("referenced packet commitment not found locally")

            if existing["delivered"]:
                # 重放：真实 IBC 中继重试同一投递，幂等拒绝。
                raise ErrAlreadyExists("packet already delivered on this channel")

            if existing["status"] == "TIMED_OUT":
                raise ErrTerminal("packet already timed out on source")

            if ch["ordering"] == "ordered":
                expected = ch["next_seq_recv"]
                seq = int(packet["sequence"])
                if seq < expected:
                    raise ErrAlreadyExists("sequence already received")
                if seq > expected:
                    raise ErrSeqGap(
                        f"expected seq {expected}, got {seq}: gap, packet must wait"
                    )
                conn.execute(
                    "UPDATE channels SET next_seq_recv=? WHERE chain_id=? AND port_id=? "
                    "AND channel_id=?",
                    (seq + 1, packet["dst_chain"], packet["dst_port"], packet["dst_channel"]),
                )
            # 无序：逐包去重（上面 delivered 检查已完成），不要求连续。

            cur = conn.execute(
                "UPDATE packets SET delivered=1, delivered_at_height=?, status='RECEIVED' "
                "WHERE id=? AND delivered=0",
                (tip["height"], existing["id"]),
            )
            if cur.rowcount != 1:
                # 条件更新失败只可能是并发的另一个投递先成功（去重）。
                raise ErrAlreadyExists("concurrent duplicate delivery")
            return {
                "result": "delivered",
                "sequence": int(packet["sequence"]),
                "ordering": ch["ordering"],
                "at_destination_height": tip["height"],
            }
        finally:
            self._finish(conn)

    # ---- 确认（Acknowledgement） ----

    def acknowledge_packet(
        self, packet: dict[str, Any], cp: dict[str, Any], proof: dict[str, Any]
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            self._txn(conn)
            ch = self._get_channel_conn(
                conn, packet["src_chain"], packet["src_port"], packet["src_channel"]
            )
            if ch["closed"]:
                raise ErrClosed("source channel is closed")
            if ch["counterparty_chain"] != packet["dst_chain"] or \
               ch["counterparty_port"] != packet["dst_port"] or \
               ch["counterparty_channel"] != packet["dst_channel"]:
                raise ErrProof("packet counterparty does not match local channel binding")
            if cp.get("chain_id") != packet["dst_chain"]:
                raise ErrProof("checkpoint chain_id does not match packet destination")

            self._verify_checkpoint_and_update_client(
                conn,
                local_chain=packet["src_chain"],
                cp=cp,
                local_cp_port=ch["counterparty_port"],
                local_cp_channel=ch["counterparty_channel"],
            )

            row = conn.execute(
                "SELECT * FROM packets WHERE src_chain=? AND src_port=? "
                "AND src_channel=? AND sequence=?",
                (packet["src_chain"], packet["src_port"], packet["src_channel"],
                 int(packet["sequence"])),
            ).fetchone()
            if row is None:
                raise ErrNotFound("packet not found")

            seq = int(packet["sequence"])
            if ch["ordering"] == "ordered":
                # 有序：确认也必须按序，且用 nextSequenceRecv > seq 证明“已收”。
                if seq < ch["next_seq_ack"]:
                    raise ErrAlreadyExists("already acknowledged")
                if seq != ch["next_seq_ack"]:
                    raise ErrSeqGap("ack sequence gap on ordered channel")
                self._check_next_seq_recv_value(
                    cp, proof, packet,
                    lambda v: v > seq,
                    f"ordered ack requires nextSequenceRecv > {seq}",
                )
            else:
                # 无序：逐包回执成员证明（值为固定 0x01）。
                rk = receipt_key(packet["dst_port"], packet["dst_channel"], seq)
                self._check_proof(cp, rk, b"\x01", proof)

            # 终态互斥：只有仍是 SENT/RECEIVED（未超时）的包能确认。
            cur = conn.execute(
                "SELECT status FROM packets WHERE id=?", (row["id"],)
            ).fetchone()
            if cur["status"] == "ACKED":
                raise ErrTerminal("packet already acknowledged")
            if cur["status"] == "TIMED_OUT":
                raise ErrTerminal("packet already timed out; ack refused")

            conn.execute(
                "UPDATE packets SET status='ACKED' WHERE id=? AND status IN ('SENT','RECEIVED')",
                (row["id"],),
            )
            if ch["ordering"] == "ordered":
                conn.execute(
                    "UPDATE channels SET next_seq_ack=? WHERE chain_id=? AND port_id=? "
                    "AND channel_id=?",
                    (seq + 1, packet["src_chain"], packet["src_port"], packet["src_channel"]),
                )
            return {
                "result": "acknowledged",
                "sequence": seq,
                "escrow_released": int(row["amount"]),
                "final_status": "ACKED",
            }
        finally:
            self._finish(conn)

    # ---- 超时退款（Timeout） ----

    def timeout_packet(
        self, packet: dict[str, Any], cp: dict[str, Any], proof: dict[str, Any]
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            self._txn(conn)
            ch = self._get_channel_conn(
                conn, packet["src_chain"], packet["src_port"], packet["src_channel"]
            )
            if ch["closed"]:
                raise ErrClosed("source channel is closed")
            if ch["counterparty_chain"] != packet["dst_chain"] or \
               ch["counterparty_port"] != packet["dst_port"] or \
               ch["counterparty_channel"] != packet["dst_channel"]:
                raise ErrProof("packet counterparty does not match local channel binding")
            if cp.get("chain_id") != packet["dst_chain"]:
                raise ErrProof("checkpoint chain_id does not match packet destination")

            stored_cp = self._verify_checkpoint_and_update_client(
                conn,
                local_chain=packet["src_chain"],
                cp=cp,
                local_cp_port=ch["counterparty_port"],
                local_cp_channel=ch["counterparty_channel"],
            )

            row = conn.execute(
                "SELECT * FROM packets WHERE src_chain=? AND src_port=? "
                "AND src_channel=? AND sequence=?",
                (packet["src_chain"], packet["src_port"], packet["src_channel"],
                 int(packet["sequence"])),
            ).fetchone()
            if row is None:
                raise ErrNotFound("packet not found")
            if row["status"] == "TIMED_OUT":
                raise ErrTerminal("packet already timed out")
            if row["status"] == "ACKED":
                raise ErrTerminal("packet already acknowledged; timeout refused")

            th, tt = int(row["timeout_height"]), int(row["timeout_time_ns"])
            if not th and not tt:
                raise ErrBadRequest("packet has no timeout set")
            timed_out = (th and stored_cp["height"] >= th) or (
                tt and stored_cp["time_ns"] >= tt
            )
            if not timed_out:
                raise ErrBadRequest(
                    f"not timed out at dest height {stored_cp['height']} "
                    f"time {stored_cp['time_ns']}"
                )

            seq = int(packet["sequence"])
            if ch["ordering"] == "ordered":
                # 有序：nextSequenceRecv <= seq 证明该序号尚未被消费。
                self._check_next_seq_recv_value(
                    cp, proof, packet,
                    lambda v: v <= seq,
                    f"ordered timeout requires nextSequenceRecv <= {seq}",
                )
            else:
                # 无序：该序号回执缺失（非成员证明，空叶子）。
                rk = receipt_key(packet["dst_port"], packet["dst_channel"], seq)
                self._check_proof(cp, rk, None, proof)

            # 终态互斥的核心条件更新：并发 ack/timeout 下只有一个成功。
            cur = conn.execute(
                "UPDATE packets SET status='TIMED_OUT' "
                "WHERE id=? AND status IN ('SENT','RECEIVED')",
                (row["id"],),
            )
            if cur.rowcount != 1:
                raise ErrTerminal("packet already in terminal state")
            return {
                "result": "refunded",
                "sequence": seq,
                "refund_amount": int(row["amount"]),
                "at_destination_height": stored_cp["height"],
                "final_status": "TIMED_OUT",
            }
        finally:
            self._finish(conn)

    def _check_next_seq_recv_value(
        self,
        cp: dict[str, Any],
        proof: dict[str, Any],
        packet: dict[str, Any],
        predicate,
        fail_msg: str,
    ) -> None:
        """验证有序通道目的端 nextSequenceRecv 成员证明，并检查其数值关系。

        proof 除标准 ``steps`` 外必须携带 ``value_hex``（中继者从查询接口取得）。
        """
        val_hex = proof.get("value_hex")
        if val_hex is None:
            raise ErrProof("ordered proof must include value_hex of nextSequenceRecv")
        try:
            val = bytes.fromhex(val_hex)
        except ValueError:
            raise ErrProof("value_hex invalid")
        if len(val) != 8:
            raise ErrProof("nextSequenceRecv value must be 8 bytes")
        proven = int.from_bytes(val, "big")
        if not predicate(proven):
            raise ErrTerminal(fail_msg + f", proven={proven}")
        key = next_seq_recv_key(packet["dst_port"], packet["dst_channel"])
        self._check_proof(cp, key, val, proof)

    # ---- 崩溃恢复 / 完整性 ----

    def integrity_check(self) -> dict[str, Any]:
        """重放校验：每个高度的快照根 == 块 app_hash，prev_hash 链连续，
        最新块签名有效。检测崩溃后的脏写/页损坏/人为篡改。"""
        conn = self._connect()
        problems: list[str] = []
        try:
            for (cid,) in conn.execute("SELECT chain_id FROM chains").fetchall():
                vk = conn.execute(
                    "SELECT verify_key_hex FROM chains WHERE chain_id=?", (cid,)
                ).fetchone()["verify_key_hex"]
                prev = GENESIS_PREV_HASH
                blocks = conn.execute(
                    "SELECT * FROM blocks WHERE chain_id=? ORDER BY height", (cid,)
                ).fetchall()
                for b in blocks:
                    snap = conn.execute(
                        "SELECT kv_json FROM kv_snapshots WHERE chain_id=? AND height=?",
                        (cid, b["height"]),
                    ).fetchone()
                    if snap is None:
                        problems.append(f"{cid}@{b['height']}: snapshot missing")
                        continue
                    kv = {
                        bytes.fromhex(k): bytes.fromhex(v)
                        for k, v in json.loads(snap["kv_json"]).items()
                    }
                    root = crypto.SparseMerkleTree(kv).root().hex()
                    if root != b["app_hash"]:
                        problems.append(
                            f"{cid}@{b['height']}: app_hash mismatch ({root} != {b['app_hash']})"
                        )
                    if b["previous_app_hash"] != prev:
                        problems.append(f"{cid}@{b['height']}: previous_app_hash chain broken")
                    prev = b["app_hash"]
                    if not crypto.verify_checkpoint_signature(
                        vk, cid, b["height"], b["time_ns"], b["app_hash"],
                        b["previous_app_hash"], b["signature_hex"],
                    ):
                        problems.append(f"{cid}@{b['height']}: signature invalid")
            return {"ok": not problems, "problems": problems}
        finally:
            self._finish(conn)
