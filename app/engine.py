# -*- coding: utf-8 -*-
"""IBC 风格状态机引擎（教学简化模型）。

模拟两条（或多条）独立链共存于一个 SQLite 库中，中继器通过 API 驱动。

生命周期：
  源链 send_packet  -> 写包承诺（SMT）+ 托管资金
  区块 commit_block -> 推进高度/时间，签名检查点（信任根的证书）
  目的链 recv       -> 验检查点签名 + 验包承诺存在性证明
                       * 已超时：拒绝（TIMED_OUT）
                       * 有序通道遇到缺口：SEQUENCE_GAP，等待补包
                       * 无序通道重复包：幂等 no-op
                       成功则写回执 + 确认
  源链 acknowledge -> 验回执/确认证明，条件更新落 ACKED 终态，删除承诺
  源链 timeout     -> 检查点证明目的端确实未收且包已超时，落 TIMED_OUT 终态并退款

终态互斥：ACKED/TIMED_OUT 由单条条件 UPDATE 决定（WHERE status='IN_FLIGHT'），
SQLite 写事务串行化保证并发确认与退款只有一个成功。
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

from . import crypto, paths, proofs as proof_mod
from .encoding import from_hex, to_hex, u64be, u64be_to_int
from .errors import *  # noqa: F401,F403
from .errors import AppError
from .smt import DEFAULTS, SparseMerkleTree

EMPTY_ROOT = DEFAULTS[0]  # 空状态树根（不是全零，全零只是空叶槽常量）
from .storage import Database, SQLiteNodeStore

DEFAULT_PORT = paths.PORT
DEFAULT_TRUSTING_NANOS = 24 * 60 * 60 * 1_000_000_000  # 24 小时
BLOCK_INTERVAL_NANOS = 10 * 1_000_000_000              # 默认出块间隔 10 秒
RECEIPT_SENTINEL = b"\x01"                             # IBC 回执哨兵字节
DEFAULT_ACK = b"success"

ORDER_ORDERED = "ORDERED"
ORDER_UNORDERED = "UNORDERED"

ST_INIT, ST_TRYOPEN, ST_OPEN, ST_CLOSED = "INIT", "TRYOPEN", "OPEN", "CLOSED"
PKT_IN_FLIGHT, PKT_DELIVERED, PKT_ACKED, PKT_TIMED_OUT = (
    "IN_FLIGHT",
    "DELIVERED",
    "ACKED",
    "TIMED_OUT",
)


class Engine:
    def __init__(self, db: Database):
        self.db = db

    # ======================================================================
    # 内部小工具
    # ======================================================================

    def _conn(self) -> sqlite3.Connection:
        return self.db.connect()

    def _smt(self, conn: sqlite3.Connection, chain_id: str) -> SparseMerkleTree:
        return SparseMerkleTree(SQLiteNodeStore(conn, chain_id))

    def _chain(self, conn: sqlite3.Connection, chain_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM chains WHERE chain_id=?", (chain_id,)
        ).fetchone()
        if not row:
            raise AppError(ERR_UNKNOWN_CHAIN, f"未知链: {chain_id}", 404)
        return row

    def _channel(
        self, conn: sqlite3.Connection, chain_id: str, channel_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM channels WHERE chain_id=? AND channel_id=?",
            (chain_id, channel_id),
        ).fetchone()
        if not row:
            raise AppError(ERR_UNKNOWN_CHANNEL, f"链 {chain_id} 上无通道 {channel_id}", 404)
        return row

    def _client_between(
        self, conn: sqlite3.Connection, local_chain: str, remote_chain: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM clients WHERE local_chain_id=? AND remote_chain_id=?",
            (local_chain, remote_chain),
        ).fetchone()
        if not row:
            raise AppError(
                ERR_UNKNOWN_CLIENT,
                f"链 {local_chain} 上没有信任 {remote_chain} 的轻客户端",
                404,
            )
        return row

    def _write_channel_value(self, conn, ch: sqlite3.Row) -> None:
        val = paths.channel_value(
            ch["state"], ch["ordering"], ch["version"],
            ch["remote_channel"], ch["remote_port"], ch["conn_id"],
        )
        self._smt(conn, ch["chain_id"]).set(paths.channel_key(ch["channel_id"]), val)

    def _verify_cp(
        self,
        conn: sqlite3.Connection,
        proof: dict,
        local_chain: str,
        remote_chain: str,
    ) -> tuple[dict, sqlite3.Row]:
        client = self._client_between(conn, local_chain, remote_chain)
        local = self._chain(conn, local_chain)
        cp = proof_mod.verify_checkpoint(
            proof,
            client["trusted_pubkey"],
            remote_chain,
            client["last_height"],
            client["last_time"],
            client["trusting_nanos"],
            now_nanos=local["time_nanos"],
        )
        return cp, client

    def _update_client_trusted(self, conn, client: sqlite3.Row, cp: dict) -> None:
        conn.execute(
            "UPDATE clients SET last_height=?, last_time=? WHERE client_id=?",
            (cp["height"], cp["time_nanos"], client["client_id"]),
        )

    # ======================================================================
    # 链与出块 / 检查点
    # ======================================================================

    def create_chain(
        self,
        chain_id: str,
        revision_number: int = 1,
        genesis_time_nanos: Optional[int] = None,
    ) -> dict:
        if not chain_id or len(chain_id) > 64:
            raise AppError(ERR_BAD_INPUT, "chain_id 非空且不超过 64 字符")
        if revision_number < 0:
            raise AppError(ERR_BAD_INPUT, "revision_number 不能为负")
        if genesis_time_nanos is None:
            genesis_time_nanos = time.time_ns()
        if genesis_time_nanos < 0:
            raise AppError(ERR_BAD_INPUT, "genesis_time_nanos 不能为负")

        sk, pk = crypto.generate_keypair()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM chains WHERE chain_id=?", (chain_id,)
            ).fetchone()
            if exists:
                raise AppError(ERR_DUPLICATE, f"链 {chain_id} 已存在", 409)
            conn.execute(
                "INSERT INTO chains(chain_id, revision_number, height, time_nanos, "
                "privkey_hex, pubkey_hex, next_commit_seq, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    chain_id,
                    revision_number,
                    0,
                    genesis_time_nanos,
                    crypto.private_key_hex(sk),
                    crypto.public_key_hex(pk),
                    0,
                    _now_iso(),
                ),
            )
            conn.commit()
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        return self.chain_info(chain_id)

    def chain_info(self, chain_id: str) -> dict:
        conn = self._conn()
        row = self._chain(conn, chain_id)
        dirty = self._has_uncommitted(conn, chain_id, row)
        return {
            "chain_id": row["chain_id"],
            "revision_number": row["revision_number"],
            "height": row["height"],
            "time_nanos": row["time_nanos"],
            "pubkey": row["pubkey_hex"],
            "uncommitted_state": dirty,
        }

    def list_chains(self) -> list[dict]:
        return [self.chain_info(r["chain_id"]) for r in self.db.query("SELECT chain_id FROM chains")]

    def _latest_checkpoint_row(self, conn, chain_id: str):
        return conn.execute(
            "SELECT * FROM checkpoints WHERE chain_id=? ORDER BY height DESC LIMIT 1",
            (chain_id,),
        ).fetchone()

    def _has_uncommitted(self, conn, chain_id: str, chain_row=None) -> bool:
        if chain_row is None:
            chain_row = self._chain(conn, chain_id)
        cp = self._latest_checkpoint_row(conn, chain_id)
        live_root = self._smt(conn, chain_id).root()
        if cp is None:
            return live_root != EMPTY_ROOT
        return live_root.hex() != cp["app_hash"]

    def commit_block(self, chain_id: str, time_nanos: Optional[int] = None) -> dict:
        """推进一个区块：高度 +1，时间单调前进，对当前 app_hash 真实签名。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            chain = self._chain(conn, chain_id)
            new_time = chain["time_nanos"] + BLOCK_INTERVAL_NANOS if time_nanos is None else time_nanos
            if new_time < chain["time_nanos"]:
                raise AppError(
                    ERR_BAD_INPUT,
                    f"区块时间必须单调不减：{new_time} < {chain['time_nanos']}",
                )
            new_height = chain["height"] + 1
            app_hash = self._smt(conn, chain_id).root().hex()
            cp = {
                "version": crypto.CHECKPOINT_VERSION,
                "chain_id": chain_id,
                "revision_number": chain["revision_number"],
                "height": new_height,
                "time_nanos": new_time,
                "app_hash": app_hash,
                "seq": new_height,
            }
            sig = crypto.sign(
                crypto.private_key_from_hex(chain["privkey_hex"]),
                crypto.checkpoint_sign_bytes(cp),
            )
            cp_out = dict(cp)
            cp_out["signature"] = to_hex(sig)
            conn.execute(
                "INSERT INTO checkpoints(chain_id, height, time_nanos, app_hash, signature, seq) "
                "VALUES(?,?,?,?,?,?)",
                (chain_id, new_height, new_time, app_hash, cp_out["signature"], new_height),
            )
            conn.execute(
                "UPDATE chains SET height=?, time_nanos=?, next_commit_seq=? WHERE chain_id=?",
                (new_height, new_time, new_height, chain_id),
            )
            conn.commit()
            return cp_out
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def latest_checkpoint(self, chain_id: str) -> dict:
        conn = self._conn()
        self._chain(conn, chain_id)
        row = self._latest_checkpoint_row(conn, chain_id)
        if not row:
            raise AppError(ERR_INVALID_CHECKPOINT, f"链 {chain_id} 尚未提交任何区块", 409)
        return self._checkpoint_dict(conn, row)

    def _checkpoint_dict(self, conn, row) -> dict:
        chain = self._chain(conn, row["chain_id"])
        return {
            "version": crypto.CHECKPOINT_VERSION,
            "chain_id": row["chain_id"],
            "revision_number": chain["revision_number"],
            "height": row["height"],
            "time_nanos": row["time_nanos"],
            "app_hash": row["app_hash"],
            "seq": row["seq"],
            "signature": row["signature"],
        }

    def get_proof(self, chain_id: str, key_hex: str) -> dict:
        """返回锚定在最新检查点上的状态证明。存在未提交状态时拒绝（真实 IBC
        只为已提交状态出证明）。"""
        key = from_hex(key_hex, "key")
        conn = self._conn()
        chain = self._chain(conn, chain_id)
        cp_row = self._latest_checkpoint_row(conn, chain_id)
        if not cp_row:
            raise AppError(ERR_INVALID_CHECKPOINT, "该链尚未提交区块，无检查点可锚定证明", 409)
        smt = self._smt(conn, chain_id)
        live = smt.root().hex()
        if live != cp_row["app_hash"]:
            raise AppError(
                "UNCOMMITTED_STATE",
                "当前状态尚未提交区块；请先 POST /chains/" + chain_id + "/commit",
                409,
            )
        p = smt.prove(key)
        return {
            "key": to_hex(p["key"]),
            "value": to_hex(p["value"]),
            "membership": p["membership"],
            "siblings": [to_hex(h) for h in p["siblings"]],
            "checkpoint": self._checkpoint_dict(conn, cp_row),
        }

    # ======================================================================
    # 轻客户端（信任根登记）
    # ======================================================================

    def create_client(
        self,
        local_chain_id: str,
        remote_chain_id: str,
        client_id: Optional[str] = None,
        trusting_period_nanos: int = DEFAULT_TRUSTING_NANOS,
    ) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            local = self._chain(conn, local_chain_id)
            remote = self._chain(conn, remote_chain_id)
            if local_chain_id == remote_chain_id:
                raise AppError(ERR_BAD_INPUT, "客户端不能信任同一条链")
            dup = conn.execute(
                "SELECT 1 FROM clients WHERE local_chain_id=? AND remote_chain_id=?",
                (local_chain_id, remote_chain_id),
            ).fetchone()
            if dup:
                raise AppError(ERR_DUPLICATE, "该方向的客户端已存在", 409)
            if trusting_period_nanos <= 0:
                raise AppError(ERR_BAD_INPUT, "trusting_period_nanos 必须为正")
            cid = client_id or f"client-{local_chain_id}-{remote_chain_id}"
            conn.execute(
                "INSERT INTO clients(client_id, local_chain_id, remote_chain_id, "
                "trusted_pubkey, trusting_nanos, last_height, last_time) "
                "VALUES(?,?,?,?,?,0,0)",
                (cid, local_chain_id, remote_chain_id, remote["pubkey_hex"], trusting_period_nanos),
            )
            conn.commit()
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        return self.client_info(cid)

    def client_info(self, client_id: str) -> dict:
        conn = self._conn()
        row = conn.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        if not row:
            raise AppError(ERR_UNKNOWN_CLIENT, f"未知客户端: {client_id}", 404)
        return dict(row)

    # ======================================================================
    # 连接（教学简化：一次建立双向 OPEN 连接端，需双向互信客户端）
    # ======================================================================

    def create_connection(
        self, conn_id: str, chain_a: str, chain_b: str
    ) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._chain(conn, chain_a)
            self._chain(conn, chain_b)
            client_a = self._client_between(conn, chain_a, chain_b)
            client_b = self._client_between(conn, chain_b, chain_a)
            if conn.execute(
                "SELECT 1 FROM connections WHERE conn_id=?", (conn_id,)
            ).fetchone():
                raise AppError(ERR_DUPLICATE, f"连接 {conn_id} 已存在", 409)
            # 两个连接端共享同一个连接编号（简化），远端互相指回
            conn.execute(
                "INSERT INTO connections(conn_id, chain_id, client_id, remote_conn_id, state) "
                "VALUES(?,?,?,?,?)",
                (conn_id, chain_a, client_a["client_id"], conn_id, ST_OPEN),
            )
            conn.execute(
                "INSERT INTO connections(conn_id, chain_id, client_id, remote_conn_id, state) "
                "VALUES(?,?,?,?,?)",
                (conn_id, chain_b, client_b["client_id"], conn_id, ST_OPEN),
            )
            conn.commit()
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        return {"conn_id": conn_id, chain_a: ST_OPEN, chain_b: ST_OPEN}

    # ======================================================================
    # 通道四次握手：Init -> TryOpen -> Ack -> Confirm
    # 每一步都把通道端写入状态树；跨链步骤必须附对端状态证明 + 已签名检查点。
    # ======================================================================

    def _load_connection(self, conn, chain_id: str, conn_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM connections WHERE chain_id=? AND conn_id=?",
            (chain_id, conn_id),
        ).fetchone()
        if not row:
            raise AppError(ERR_UNKNOWN_CONNECTION, f"链 {chain_id} 上无连接 {conn_id}", 404)
        return row

    def _insert_channel(
        self, conn, *, chain_id, channel_id, conn_id, state, ordering, version,
        remote_channel,
    ) -> sqlite3.Row:
        self._load_connection(conn, chain_id, conn_id)
        if conn.execute(
            "SELECT 1 FROM channels WHERE chain_id=? AND channel_id=?",
            (chain_id, channel_id),
        ).fetchone():
            raise AppError(ERR_DUPLICATE, f"通道 {channel_id} 在链 {chain_id} 已存在", 409)
        conn.execute(
            "INSERT INTO channels(channel_id, chain_id, port_id, conn_id, state, ordering, "
            "version, remote_channel, remote_port, next_seq_send, next_seq_recv, next_seq_ack, "
            "counterparty_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (channel_id, chain_id, DEFAULT_PORT, conn_id, state, ordering, version,
             remote_channel, DEFAULT_PORT, 1, 1, 1, ""),
        )
        ch = self._channel(conn, chain_id, channel_id)
        self._write_channel_value(conn, ch)
        return ch

    def channel_open_init(
        self, chain_id: str, channel_id: str, conn_id: str,
        ordering: str, version: str, remote_channel_id: str,
    ) -> dict:
        ordering = _ordering(ordering)
        if not version:
            raise AppError(ERR_BAD_INPUT, "version 不能为空")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ch = self._insert_channel(
                conn, chain_id=chain_id, channel_id=channel_id, conn_id=conn_id,
                state=ST_INIT, ordering=ordering, version=version,
                remote_channel=remote_channel_id,
            )
            conn.commit()
            return self.channel_info(chain_id, channel_id)
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def _proven_channel_value(self, proof: dict, cp: dict, expected_remote_id: str) -> dict:
        """验证对端通道端存在性证明并解析其 JSON 值。"""
        key = paths.channel_key(expected_remote_id)
        val = proof_mod.verify_membership(proof, cp["app_hash"], key)
        try:
            return json.loads(val.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            raise AppError(ERR_PROOF_VALUE, f"对端通道状态值无法解析: {e}") from e

    def channel_open_try(
        self, chain_id: str, channel_id: str, conn_id: str,
        ordering: str, version: str, remote_channel_id: str,
        counterparty_version: str, proof_init: dict,
    ) -> dict:
        ordering = _ordering(ordering)
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            c = self._load_connection(conn, chain_id, conn_id)
            remote_chain = conn.execute(
                "SELECT remote_chain_id FROM clients WHERE client_id=?", (c["client_id"],)
            ).fetchone()[0]
            cp, client = self._verify_cp(conn, proof_init, chain_id, remote_chain)
            remote_val = self._proven_channel_value(proof_init, cp, remote_channel_id)
            if remote_val.get("state") != ST_INIT:
                raise AppError(ERR_CHANNEL_STATE, f"对端通道状态须为 INIT，实际 {remote_val.get('state')}")
            if remote_val.get("ordering") != ordering:
                raise AppError(
                    ERR_ORDERING_MISMATCH,
                    f"顺序模式不匹配：本地 {ordering} / 对端 {remote_val.get('ordering')}",
                )
            # 对端 INIT 中的 counterparty_channel 必须就是本地将要使用的编号
            if remote_val.get("counterparty_channel") != channel_id:
                raise AppError(ERR_PROOF_KEY, "对端通道绑定的对端编号与本地通道编号不一致")
            if remote_val.get("version") != counterparty_version:
                raise AppError(
                    ERR_VERSION_MISMATCH,
                    f"版本不匹配：对端 INIT 版本 {remote_val.get('version')}，"
                    f"声称 {counterparty_version}",
                )
            if version != counterparty_version:
                raise AppError(
                    ERR_VERSION_MISMATCH,
                    f"版本协商失败：本地 {version} / 对端 {counterparty_version}",
                )
            self._insert_channel(
                conn, chain_id=chain_id, channel_id=channel_id, conn_id=conn_id,
                state=ST_TRYOPEN, ordering=ordering, version=version,
                remote_channel=remote_channel_id,
            )
            conn.execute(
                "UPDATE channels SET counterparty_version=? WHERE chain_id=? AND channel_id=?",
                (counterparty_version, chain_id, channel_id),
            )
            ch = self._channel(conn, chain_id, channel_id)
            self._write_channel_value(conn, ch)
            self._update_client_trusted(conn, client, cp)
            conn.commit()
            return self.channel_info(chain_id, channel_id)
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def channel_open_ack(
        self, chain_id: str, channel_id: str, proof_try: dict
    ) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ch = self._channel(conn, chain_id, channel_id)
            if ch["state"] != ST_INIT:
                raise AppError(ERR_CHANNEL_STATE, f"ChanOpenAck 要求 INIT，实际 {ch['state']}")
            c = self._load_connection(conn, chain_id, ch["conn_id"])
            remote_chain = conn.execute(
                "SELECT remote_chain_id FROM clients WHERE client_id=?", (c["client_id"],)
            ).fetchone()[0]
            cp, client = self._verify_cp(conn, proof_try, chain_id, remote_chain)
            remote_val = self._proven_channel_value(proof_try, cp, ch["remote_channel"])
            if remote_val.get("state") != ST_TRYOPEN:
                raise AppError(ERR_CHANNEL_STATE, f"对端须为 TRYOPEN，实际 {remote_val.get('state')}")
            if remote_val.get("ordering") != ch["ordering"]:
                raise AppError(ERR_ORDERING_MISMATCH, "顺序模式不匹配")
            if remote_val.get("version") != ch["version"]:
                raise AppError(
                    ERR_VERSION_MISMATCH,
                    f"对端选定版本 {remote_val.get('version')} 与本地 {ch['version']} 不一致",
                )
            conn.execute(
                "UPDATE channels SET state=?, counterparty_version=? "
                "WHERE chain_id=? AND channel_id=?",
                (ST_OPEN, remote_val.get("version", ""), chain_id, channel_id),
            )
            ch = self._channel(conn, chain_id, channel_id)
            self._write_channel_value(conn, ch)
            self._update_client_trusted(conn, client, cp)
            conn.commit()
            return self.channel_info(chain_id, channel_id)
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def channel_open_confirm(
        self, chain_id: str, channel_id: str, proof_ack: dict
    ) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ch = self._channel(conn, chain_id, channel_id)
            if ch["state"] != ST_TRYOPEN:
                raise AppError(ERR_CHANNEL_STATE, f"ChanOpenConfirm 要求 TRYOPEN，实际 {ch['state']}")
            c = self._load_connection(conn, chain_id, ch["conn_id"])
            remote_chain = conn.execute(
                "SELECT remote_chain_id FROM clients WHERE client_id=?", (c["client_id"],)
            ).fetchone()[0]
            cp, client = self._verify_cp(conn, proof_ack, chain_id, remote_chain)
            remote_val = self._proven_channel_value(proof_ack, cp, ch["remote_channel"])
            if remote_val.get("state") != ST_OPEN:
                raise AppError(ERR_CHANNEL_STATE, f"对端须为 OPEN，实际 {remote_val.get('state')}")
            if remote_val.get("ordering") != ch["ordering"]:
                raise AppError(ERR_ORDERING_MISMATCH, "顺序模式不匹配")
            conn.execute(
                "UPDATE channels SET state=? WHERE chain_id=? AND channel_id=?",
                (ST_OPEN, chain_id, channel_id),
            )
            ch = self._channel(conn, chain_id, channel_id)
            self._write_channel_value(conn, ch)
            self._update_client_trusted(conn, client, cp)
            conn.commit()
            return self.channel_info(chain_id, channel_id)
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def channel_info(self, chain_id: str, channel_id: str) -> dict:
        conn = self._conn()
        ch = self._channel(conn, chain_id, channel_id)
        return {
            "chain_id": ch["chain_id"],
            "channel_id": ch["channel_id"],
            "port_id": ch["port_id"],
            "conn_id": ch["conn_id"],
            "state": ch["state"],
            "ordering": ch["ordering"],
            "version": ch["version"],
            "counterparty_channel": ch["remote_channel"],
            "counterparty_port": ch["remote_port"],
            "counterparty_version": ch["counterparty_version"],
            "next_seq_send": ch["next_seq_send"],
            "next_seq_recv": ch["next_seq_recv"],
            "next_seq_ack": ch["next_seq_ack"],
        }

    def channel_close(self, chain_id: str, channel_id: str) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ch = self._channel(conn, chain_id, channel_id)
            if ch["state"] == ST_CLOSED:
                raise AppError(ERR_CHANNEL_CLOSED, "通道已关闭", 409)
            conn.execute(
                "UPDATE channels SET state=? WHERE chain_id=? AND channel_id=?",
                (ST_CLOSED, chain_id, channel_id),
            )
            ch = self._channel(conn, chain_id, channel_id)
            self._write_channel_value(conn, ch)
            conn.commit()
            return self.channel_info(chain_id, channel_id)
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    # ======================================================================
    # 数据包：发送
    # ======================================================================

    def send_packet(
        self,
        chain_id: str,
        channel_id: str,
        data_hex: str,
        timeout_height: int,
        timeout_time_nanos: int,
        amount: int = 0,
        sender: str = "alice",
        receiver: str = "bob",
    ) -> dict:
        data = from_hex(data_hex, "data")
        timeout_height = _nonneg_int(timeout_height, "timeout_height")
        timeout_time_nanos = _nonneg_int(timeout_time_nanos, "timeout_time_nanos")
        amount = _nonneg_int(amount, "amount")
        if timeout_height == 0 and timeout_time_nanos == 0:
            raise AppError(ERR_BAD_INPUT, "必须至少设置一种超时（高度或时间）")
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            src = self._channel(conn, chain_id, channel_id)
            if src["state"] != ST_OPEN:
                raise AppError(ERR_CHANNEL_STATE, f"源通道未 OPEN（{src['state']}），不能发包")
            dst = self._channel(conn, _remote_chain_of(conn, src), src["remote_channel"])
            if dst["ordering"] != src["ordering"]:
                raise AppError(ERR_ORDERING_MISMATCH, "两端顺序模式不一致")
            dst_chain_row = self._chain(conn, dst["chain_id"])
            seq = src["next_seq_send"]
            commitment = crypto.packet_commitment(
                dst_chain_row["revision_number"],
                timeout_height,
                timeout_time_nanos,
                data,
            )
            conn.execute(
                "INSERT INTO packets(chain_id, channel_id, sequence, dst_chain_id, dst_channel_id, "
                "data, timeout_revision_number, timeout_height, timeout_time, amount, sender, "
                "receiver, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (chain_id, channel_id, seq, dst["chain_id"], dst["channel_id"], data_hex,
                 dst_chain_row["revision_number"], timeout_height,
                 timeout_time_nanos, amount, sender, receiver, PKT_IN_FLIGHT),
            )
            smt = self._smt(conn, chain_id)
            smt.set(paths.packet_commitment_key(channel_id, seq), commitment)
            smt.set(paths.next_seq_send_key(channel_id, seq), u64be(seq + 1))
            conn.execute(
                "UPDATE channels SET next_seq_send=? WHERE chain_id=? AND channel_id=?",
                (seq + 1, chain_id, channel_id),
            )
            if amount:
                self._move_escrow(conn, chain_id, channel_id, sender, "__escrow__", amount)
            conn.commit()
            return self._packet_descriptor(chain_id, channel_id, seq)
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def _move_escrow(self, conn, chain_id, channel_id, frm: str, to: str, amount: int) -> None:
        # 简化模型：只对托管池 __escrow__ 做严格余额校验（它的钱只能来自之前的托管），
        # 外部用户账户视为有链外余额，允许为记账上的负值。
        if frm == "__escrow__":
            r = conn.execute(
                "SELECT amount FROM escrow WHERE chain_id=? AND channel_id=? AND account=?",
                (chain_id, channel_id, frm),
            ).fetchone()
            if (r[0] if r else 0) < amount:
                raise AppError("INSUFFICIENT_ESCROW", "托管池余额不足，无法退款")
        self._add(conn, chain_id, channel_id, frm, -amount, strict=(frm == "__escrow__"))
        self._add(conn, chain_id, channel_id, to, amount, strict=(to == "__escrow__"))

    def _add(self, conn, chain_id, channel_id, account, delta, *, strict: bool = False) -> None:
        row = conn.execute(
            "SELECT amount FROM escrow WHERE chain_id=? AND channel_id=? AND account=?",
            (chain_id, channel_id, account),
        ).fetchone()
        new = (row[0] if row else 0) + delta
        if new < 0 and strict:
            raise AppError("INSUFFICIENT_ESCROW", f"{account} 余额将变为负")
        conn.execute(
            "INSERT INTO escrow(chain_id, channel_id, account, amount) VALUES(?,?,?,?) "
            "ON CONFLICT(chain_id, channel_id, account) DO UPDATE SET amount=excluded.amount",
            (chain_id, channel_id, account, new),
        )

    def _packet_descriptor(self, chain_id, channel_id, seq, revision_number=None) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT * FROM packets WHERE chain_id=? AND channel_id=? AND sequence=?",
            (chain_id, channel_id, seq),
        ).fetchone()
        if not row:
            raise AppError(ERR_UNKNOWN_PACKET, "包不存在", 404)
        return {
            "source_chain": row["chain_id"],
            "source_channel": row["channel_id"],
            "source_port": DEFAULT_PORT,
            "sequence": row["sequence"],
            "destination_chain": row["dst_chain_id"],
            "destination_channel": row["dst_channel_id"],
            "destination_port": DEFAULT_PORT,
            "data": row["data"],
            "timeout_revision_number": row["timeout_revision_number"],
            "timeout_height": row["timeout_height"],
            "timeout_time_nanos": row["timeout_time"],
            "amount": row["amount"],
            "sender": row["sender"],
            "receiver": row["receiver"],
            "status": row["status"],
        }

    def packet_info(self, chain_id: str, channel_id: str, seq: int) -> dict:
        return self._packet_descriptor(chain_id, channel_id, seq)

    def list_packets(self, chain_id: str, channel_id: Optional[str] = None) -> list[dict]:
        conn = self._conn()
        self._chain(conn, chain_id)
        if channel_id:
            rows = conn.execute(
                "SELECT sequence FROM packets WHERE chain_id=? AND channel_id=? ORDER BY sequence",
                (chain_id, channel_id),
            ).fetchall()
            return [self._packet_descriptor(chain_id, channel_id, r["sequence"]) for r in rows]
        rows = conn.execute(
            "SELECT channel_id, sequence FROM packets WHERE chain_id=? ORDER BY channel_id, sequence",
            (chain_id,),
        ).fetchall()
        return [self._packet_descriptor(chain_id, r["channel_id"], r["sequence"]) for r in rows]

    # ======================================================================
    # 数据包：目的链接收
    # ======================================================================

    def recv_packet(self, pkt: dict, proof_commitment: dict, ack_hex: Optional[str] = None) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            dst_ch_id = _require(pkt, "destination_chain")
            dst_ch_num = _require(pkt, "destination_channel")
            dst_ch = self._channel(conn, dst_ch_id, dst_ch_num)
            src_ch_id = _require(pkt, "source_chain")
            src_ch_num = _require(pkt, "source_channel")
            seq = _posint(_require(pkt, "sequence"), "sequence")
            data = from_hex(_require(pkt, "data"), "data")
            rev_num = _nonneg_int(_require(pkt, "timeout_revision_number"), "timeout_revision_number")
            th = _nonneg_int(_require(pkt, "timeout_height"), "timeout_height")
            tt = _nonneg_int(_require(pkt, "timeout_time_nanos"), "timeout_time_nanos")

            # 包必须确实发往本通道（对端绑定校验）
            if dst_ch["remote_channel"] != src_ch_num:
                raise AppError(ERR_PROOF_KEY, "包的源通道与本地通道绑定的对端不一致")
            src_ch = self._channel(conn, src_ch_id, src_ch_num)
            if src_ch["remote_channel"] != dst_ch_num:
                raise AppError(ERR_PROOF_KEY, "源通道绑定的对端通道与包不一致")
            if dst_ch["state"] == ST_CLOSED:
                raise AppError(ERR_CHANNEL_CLOSED, "目的通道已关闭，不能接收数据包")
            if dst_ch["state"] != ST_OPEN:
                raise AppError(ERR_CHANNEL_STATE, f"目的通道未 OPEN（{dst_ch['state']}）")

            # 1) 验证源链检查点（签名 + 新鲜度）
            cp, client = self._verify_cp(conn, proof_commitment, dst_ch_id, src_ch_id)

            # 2) 验证包承诺存在性证明：重算承诺值必须与状态树中的值一致
            expected_commit = crypto.packet_commitment(rev_num, th, tt, data)
            proof_mod.verify_membership(
                proof_commitment,
                cp["app_hash"],
                paths.packet_commitment_key(src_ch_num, seq),
                expected_commit,
            )

            # 3) 超时判定（相对目的链自身的高度/时间；>= 边界即超时）
            local_chain = self._chain(conn, dst_ch_id)
            if rev_num != local_chain["revision_number"]:
                raise AppError(
                    ERR_BAD_INPUT,
                    f"超时高度版本号 {rev_num} 与目的链版本 {local_chain['revision_number']} 不符",
                )
            if th and local_chain["height"] >= th:
                raise AppError(
                    ERR_TIMEOUT,
                    f"包已按高度超时：目的链高度 {local_chain['height']} >= 超时高度 {th}",
                    409,
                )
            if tt and local_chain["time_nanos"] >= tt:
                raise AppError(
                    ERR_TIMEOUT,
                    f"包已按时间超时：目的链时间 {local_chain['time_nanos']} >= 超时时间 {tt}",
                    409,
                )

            ack = from_hex(ack_hex, "ack") if ack_hex else DEFAULT_ACK
            smt = self._smt(conn, dst_ch_id)
            result_extra: dict[str, Any] = {}

            if dst_ch["ordering"] == ORDER_ORDERED:
                nsn = dst_ch["next_seq_recv"]
                if seq < nsn:
                    # 已交付过：IBC ordered 通道上的重放，幂等成功
                    result_extra = {"duplicate": True}
                elif seq > nsn:
                    raise AppError(
                        ERR_SEQ_GAP,
                        f"有序通道期望序号 {nsn}，收到 {seq}：存在缺口，等待前序包",
                        409,
                    )
                else:
                    nsn = seq + 1
                    conn.execute(
                        "UPDATE channels SET next_seq_recv=? WHERE chain_id=? AND channel_id=?",
                        (nsn, dst_ch_id, dst_ch_num),
                    )
                    smt.set(paths.next_seq_recv_key(dst_ch_num), u64be(nsn))
                    self._record_receipt(conn, dst_ch_id, dst_ch_num, seq)
            else:
                exists = conn.execute(
                    "SELECT 1 FROM packet_receipts WHERE chain_id=? AND channel_id=? AND sequence=?",
                    (dst_ch_id, dst_ch_num, seq),
                ).fetchone()
                if exists:
                    result_extra = {"duplicate": True}
                else:
                    smt.set(paths.packet_receipt_key(dst_ch_num, seq), RECEIPT_SENTINEL)
                    self._record_receipt(conn, dst_ch_id, dst_ch_num, seq)

            # 目的链写确认（IBC WriteAcknowledgement）：
            # 状态树存承诺值 sha256(sha256(ack))，原始 ack 另行留档
            if not result_extra.get("duplicate"):
                smt.set(paths.packet_ack_key(dst_ch_num, seq), crypto.ack_commitment(ack))
                conn.execute(
                    "INSERT INTO packet_acks(chain_id, channel_id, sequence, ack) "
                    "VALUES(?,?,?,?) ON CONFLICT(chain_id, channel_id, sequence) "
                    "DO UPDATE SET ack=excluded.ack",
                    (dst_ch_id, dst_ch_num, seq, ack.hex()),
                )
                self._mark_packet_status(conn, src_ch_id, src_ch_num, seq, PKT_DELIVERED)

            self._update_client_trusted(conn, client, cp)
            conn.commit()
            return {
                "result": "received" if not result_extra else "already_received",
                "destination_chain": dst_ch_id,
                "destination_channel": dst_ch_num,
                "sequence": seq,
                "ack": ack.hex(),
                **result_extra,
            }
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def _record_receipt(self, conn, chain_id, channel_id, seq) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO packet_receipts(chain_id, channel_id, sequence, received) "
            "VALUES(?,?,?,1)",
            (chain_id, channel_id, seq),
        )

    def _mark_packet_status(self, conn, chain_id, channel_id, seq, status) -> None:
        # 包行在源链；作为跨链运营索引更新（权威状态以 SMT 证明为准）
        conn.execute(
            "UPDATE packets SET status=? WHERE chain_id=? AND channel_id=? AND sequence=?",
            (status, chain_id, channel_id, seq),
        )

    # ======================================================================
    # 数据包：源链确认（终态一）
    # ======================================================================

    def acknowledge_packet(self, pkt: dict, ack_hex: str, proofs: dict) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            src_chain = _require(pkt, "source_chain")
            src_chan = _require(pkt, "source_channel")
            dst_chain = _require(pkt, "destination_chain")
            dst_chan = _require(pkt, "destination_channel")
            seq = _posint(_require(pkt, "sequence"), "sequence")
            ack = from_hex(ack_hex, "ack")

            row = conn.execute(
                "SELECT * FROM packets WHERE chain_id=? AND channel_id=? AND sequence=?",
                (src_chain, src_chan, seq),
            ).fetchone()
            if not row:
                raise AppError(ERR_UNKNOWN_PACKET, "源链上找不到该包承诺", 404)

            proof_ack = _subproof(proofs, "proof_ack")
            cp, client = self._verify_cp(conn, proof_ack, src_chain, dst_chain)
            dst_ch = self._channel(conn, dst_chain, dst_chan)

            # 确认内容必须与目的链写入的一致
            proof_mod.verify_membership(
                proof_ack, cp["app_hash"],
                paths.packet_ack_key(dst_chan, seq),
                crypto.ack_commitment(ack),
            )

            if dst_ch["ordering"] == ORDER_ORDERED:
                proof_nsn = _subproof(proofs, "proof_next_seq_recv")
                cp2 = cp
                if proof_nsn.get("checkpoint") is not None and proof_nsn is not proof_ack:
                    cp2, client2 = self._verify_cp(conn, proof_nsn, src_chain, dst_chain)
                nsn = self._verified_next_seq_recv(proof_nsn, cp2["app_hash"], dst_chan)
                if nsn < seq + 1:
                    raise AppError(
                        ERR_PROOF_VALUE,
                        f"有序通道 nextSeqRecv={nsn} 尚未覆盖序号 {seq}",
                    )
            else:
                proof_rcpt = _subproof(proofs, "proof_receipt")
                cp2 = cp
                if proof_rcpt is not proof_ack:
                    cp2, _ = self._verify_cp(conn, proof_rcpt, src_chain, dst_chain)
                proof_mod.verify_membership(
                    proof_rcpt, cp2["app_hash"],
                    paths.packet_receipt_key(dst_chan, seq),
                    RECEIPT_SENTINEL,
                )

            # 终态互斥：只有 IN_FLIGHT 能落 ACKED
            cur = self._finalize(conn, row, PKT_ACKED, delete_commitment=True)
            conn.execute(
                "INSERT INTO packet_acks(chain_id, channel_id, sequence, ack) "
                "VALUES(?,?,?,?) ON CONFLICT(chain_id, channel_id, sequence) "
                "DO UPDATE SET ack=excluded.ack",
                (src_chain, src_chan, seq, ack.hex()),
            )
            conn.execute(
                "UPDATE channels SET next_seq_ack=next_seq_ack+1 WHERE chain_id=? AND channel_id=?",
                (src_chain, src_chan),
            )
            self._update_client_trusted(conn, client, cp)
            conn.commit()
            return {
                "result": "acknowledged",
                "source_chain": src_chain,
                "source_channel": src_chan,
                "sequence": seq,
                "previous_status": cur,
                "final_status": PKT_ACKED,
            }
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    def _verified_next_seq_recv(self, proof: dict, app_hash_hex: str, channel_id: str) -> int:
        """验证有序通道 nextSeqRecv；键尚未写入时按非成员证明处理，值默认 1。"""
        key = paths.next_seq_recv_key(channel_id)
        if proof.get("membership") is False or not proof.get("value"):
            proof_mod.verify_non_membership(proof, app_hash_hex, key)
            return 1
        val = proof_mod.verify_membership(proof, app_hash_hex, key)
        return u64be_to_int(val)

    def _finalize(self, conn, row, status: str, *, delete_commitment: bool) -> str:
        """条件更新落终态。返回更新前状态；若非 IN_FLIGHT/DELIVERED 则拒绝。

        DELIVERED 只是“目的链已接收”的运营索引（由跨链回写），源链终态
        只有 ACKED / TIMED_OUT 两种，二者互斥。
        """
        cur_row = conn.execute(
            "SELECT status FROM packets WHERE chain_id=? AND channel_id=? AND sequence=?",
            (row["chain_id"], row["channel_id"], row["sequence"]),
        ).fetchone()
        cur = cur_row["status"]
        if cur in (PKT_ACKED, PKT_TIMED_OUT):
            raise AppError(
                ERR_PACKET_FINALIZED,
                f"包已是终态 {cur}，不能再转为 {status}（确认与退款互斥）",
                409,
            )
        rc = conn.execute(
            "UPDATE packets SET status=? WHERE chain_id=? AND channel_id=? AND sequence=? "
            "AND status IN ('IN_FLIGHT','DELIVERED')",
            (status, row["chain_id"], row["channel_id"], row["sequence"]),
        )
        if rc.rowcount != 1:
            # 并发竞态：另一个事务抢先落了终态
            raise AppError(ERR_PACKET_FINALIZED, "并发竞争：包已被另一流程终结", 409)
        if delete_commitment:
            self._smt(conn, row["chain_id"]).delete(
                paths.packet_commitment_key(row["channel_id"], row["sequence"])
            )
        return cur

    # ======================================================================
    # 数据包：源链超时退款（终态二）
    # ======================================================================

    def timeout_packet(self, pkt: dict, proofs: dict) -> dict:
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            src_chain = _require(pkt, "source_chain")
            src_chan = _require(pkt, "source_channel")
            dst_chain = _require(pkt, "destination_chain")
            dst_chan = _require(pkt, "destination_channel")
            seq = _posint(_require(pkt, "sequence"), "sequence")

            row = conn.execute(
                "SELECT * FROM packets WHERE chain_id=? AND channel_id=? AND sequence=?",
                (src_chain, src_chan, seq),
            ).fetchone()
            if not row:
                raise AppError(ERR_UNKNOWN_PACKET, "源链上找不到该包承诺", 404)
            src_ch = self._channel(conn, src_chain, src_chan)
            dst_ch = self._channel(conn, dst_chain, dst_chan)

            proof_main = _subproof(proofs, "proof_unreceived", required=False)
            proof_closed = _subproof(proofs, "proof_channel_closed", required=False)
            if proof_main is None and proof_closed is None:
                raise AppError(ERR_BAD_INPUT, "超时必须附 proof_unreceived 或 proof_channel_closed")

            def _check(cp_dict, prov):
                # 超时成立与否以目的链共识状态（检查点高度/时间）为准
                dst_rev = cp_dict["revision_number"]
                if row["timeout_revision_number"] != dst_rev:
                    raise AppError(
                        ERR_BAD_INPUT,
                        f"超时版本号 {row['timeout_revision_number']} 与目的链版本 {dst_rev} 不符",
                    )
                th, tt = row["timeout_height"], row["timeout_time"]
                if th and cp_dict["height"] >= th:
                    pass
                elif tt and cp_dict["time_nanos"] >= tt:
                    pass
                else:
                    raise AppError(
                        ERR_NOT_TIMEOUT,
                        f"在检查点高度 {cp_dict['height']} / 时间 {cp_dict['time_nanos']} "
                        f"上包尚未超时（阈值 H={th}, T={tt}）",
                        409,
                    )
                return prov

            if dst_ch["state"] == ST_CLOSED or proof_closed is not None:
                # 通道已关闭路径：证明目的通道端为 CLOSED 即可（有序/无序均允许）
                prov = proof_closed or proof_main
                cp, client = self._verify_cp(conn, prov, src_chain, dst_chain)
                _check(cp, prov)
                val = proof_mod.verify_membership(
                    prov, cp["app_hash"], paths.channel_key(dst_chan)
                )
                state = json.loads(val.decode()).get("state")
                if state != ST_CLOSED:
                    raise AppError(ERR_PROOF_VALUE, f"期望通道 CLOSED 证明，实际状态 {state}")
            elif dst_ch["ordering"] == ORDER_ORDERED:
                cp, client = self._verify_cp(conn, proof_main, src_chain, dst_chain)
                _check(cp, proof_main)
                nsn = self._verified_next_seq_recv(
                    proof_main, cp["app_hash"], dst_chan
                )
                if nsn >= seq + 1:
                    raise AppError(
                        ERR_PROOF_VALUE,
                        f"有序通道 nextSeqRecv={nsn} 已覆盖序号 {seq}，包已被接收，不能超时",
                    )
            else:
                cp, client = self._verify_cp(conn, proof_main, src_chain, dst_chain)
                _check(cp, proof_main)
                proof_mod.verify_non_membership(
                    proof_main, cp["app_hash"], paths.packet_receipt_key(dst_chan, seq)
                )

            self._finalize(conn, row, PKT_TIMED_OUT, delete_commitment=True)
            refunded = 0
            if row["amount"]:
                self._add(conn, src_chain, src_chan, "__escrow__", -row["amount"], strict=True)
                self._add(conn, src_chain, src_chan, row["sender"], row["amount"])
                refunded = row["amount"]
            # 有序通道超时会连带关闭本端通道（IBC 语义）
            if src_ch["ordering"] == ORDER_ORDERED and src_ch["state"] != ST_CLOSED:
                conn.execute(
                    "UPDATE channels SET state=? WHERE chain_id=? AND channel_id=?",
                    (ST_CLOSED, src_chain, src_chan),
                )
                ch = self._channel(conn, src_chain, src_chan)
                self._write_channel_value(conn, ch)
            self._update_client_trusted(conn, client, cp)
            conn.commit()
            return {
                "result": "timed_out",
                "source_chain": src_chain,
                "source_channel": src_chan,
                "sequence": seq,
                "refunded_amount": refunded,
                "final_status": PKT_TIMED_OUT,
            }
        except AppError:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise

    # ======================================================================
    # 余额 / 启动完整性检查（崩溃恢复）
    # ======================================================================

    def escrow_accounts(self, chain_id: str, channel_id: Optional[str] = None) -> list[dict]:
        conn = self._conn()
        self._chain(conn, chain_id)
        if channel_id:
            rows = conn.execute(
                "SELECT channel_id, account, amount FROM escrow WHERE chain_id=? AND channel_id=?",
                (chain_id, channel_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT channel_id, account, amount FROM escrow WHERE chain_id=?", (chain_id,)
            ).fetchall()
        return [dict(r) for r in rows]

    def verify_integrity(self) -> dict:
        """启动时恢复检查：
        1. 对每条链上的全部已存检查点重新验签（检测签名信任根被破坏）；
        2. 用 smt_kv 全量重放重建参考树，与节点存储算出的根比对（检测半写入/篡改）；
        3. 比较最新检查点 app_hash 与实时根（差异仅可能来自未提交但已持久化的操作）。
        """
        conn = self._conn()
        report: dict[str, Any] = {"chains": []}
        for c in conn.execute("SELECT * FROM chains").fetchall():
            chain_id = c["chain_id"]
            # 1) 检查点验签
            cps = conn.execute(
                "SELECT * FROM checkpoints WHERE chain_id=? ORDER BY height", (chain_id,)
            ).fetchall()
            last_cp = None
            for r in cps:
                cp = self._checkpoint_dict(conn, r)
                signed = {k: v for k, v in cp.items() if k != "signature"}
                ok = crypto.verify_signature(
                    crypto.public_key_from_hex(c["pubkey_hex"]),
                    from_hex(cp["signature"], "signature"),
                    crypto.checkpoint_sign_bytes(signed),
                )
                if not ok:
                    raise AppError(
                        "INTEGRITY_FAILURE",
                        f"链 {chain_id} 高度 {r['height']} 的检查点签名无效（存储可能被篡改）",
                        500,
                    )
                last_cp = cp

            # 2) 从 kv 全量重建参考根
            ref = SparseMerkleTree(_MemoryStoreFromRows(conn, chain_id))
            ref_root = ref.root()
            live_root = self._smt(conn, chain_id).root()
            if ref_root != live_root:
                raise AppError(
                    "INTEGRITY_FAILURE",
                    f"链 {chain_id} 状态树内部不一致：kv 重建根 {ref_root.hex()} "
                    f"!= 节点根 {live_root.hex()}（崩溃半写入或篡改）",
                    500,
                )

            info = {
                "chain_id": chain_id,
                "height": c["height"],
                "checkpoints": len(cps),
                "live_app_hash": live_root.hex(),
                "last_checkpoint_app_hash": last_cp["app_hash"] if last_cp else None,
                "uncommitted_state": (last_cp is None and live_root != EMPTY_ROOT)
                or (last_cp is not None and last_cp["app_hash"] != live_root.hex()),
            }
            report["chains"].append(info)
        return report


class _MemoryStoreFromRows:
    """只读 NodeStore：内部节点全部回落默认哈希（重建时按需重算），
    根节点取自持久化 smt_nodes，kv 来自全表扫描。"""

    def __init__(self, conn, chain_id: str):
        self.kv = {
            r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM smt_kv WHERE chain_id=?", (chain_id,))
        }
        root = conn.execute(
            "SELECT hash FROM smt_nodes WHERE chain_id=? AND node_key=?",
            (chain_id, b"\x00root"),
        ).fetchone()
        self.root = root[0] if root else None

    def get_node(self, key):
        if key == b"\x00root":
            return self.root
        return None  # 其余内部节点在重建时按默认空哈希处理

    def put_node(self, key, value_hash):
        raise RuntimeError("只读参考存储")

    def delete_node(self, key):
        raise RuntimeError("只读参考存储")

    def get_value(self, key):
        return self.kv.get(key)

    def put_value(self, key, value):
        raise RuntimeError("只读参考存储")

    def delete_value(self, key):
        raise RuntimeError("只读参考存储")


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def _remote_chain_of(conn, channel_row) -> str:
    """通过本端通道 -> 连接 -> 客户端，找到对端链编号。"""
    row = conn.execute(
        "SELECT cl.remote_chain_id AS remote FROM connections c "
        "JOIN clients cl ON cl.client_id = c.client_id "
        "WHERE c.chain_id=? AND c.conn_id=?",
        (channel_row["chain_id"], channel_row["conn_id"]),
    ).fetchone()
    if not row:
        raise AppError(ERR_UNKNOWN_CONNECTION, "通道的连接不存在", 404)
    return row["remote"]


def _require(d: dict, key: str):
    if not isinstance(d, dict) or key not in d or d[key] is None:
        raise AppError(ERR_BAD_INPUT, f"缺少字段: {key}")
    return d[key]


def _nonneg_int(v, name: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        raise AppError(ERR_BAD_INPUT, f"{name} 必须是非负整数")
    return v


def _posint(v, name: str) -> int:
    v = _nonneg_int(v, name)
    if v <= 0:
        raise AppError(ERR_BAD_INPUT, f"{name} 必须为正整数")
    return v


def _ordering(v: str) -> str:
    if v not in (ORDER_ORDERED, ORDER_UNORDERED):
        raise AppError(ERR_BAD_INPUT, f"ordering 必须是 {ORDER_ORDERED} 或 {ORDER_UNORDERED}")
    return v


def _subproof(proofs: dict, key: str, *, required: bool = True):
    if not isinstance(proofs, dict):
        raise AppError(ERR_BAD_INPUT, "proofs 必须是对象")
    p = proofs.get(key)
    if p is None and required:
        raise AppError(ERR_BAD_INPUT, f"缺少证明 {key}")
    return p


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
