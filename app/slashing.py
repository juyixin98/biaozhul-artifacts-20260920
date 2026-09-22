"""核心领域逻辑：链/验证者管理、epoch 快照冻结、证据归并、幂等惩罚。

关键不变量：
1. 双签按 (chain_id, validator, round) 识别；相同内容的重复票不是双签。
2. 同一冲突键只产生一份证据（DB UNIQUE + 行级事务锁双保险），只处罚一次。
3. 证据 ID 由「规范排序后的两份冲突票」哈希得出，逆序到达 ID 相同。
4. 惩罚引用 epoch 边界冻结的不可变快照；后续委托只改当前权益，改不了历史基数。
"""
from __future__ import annotations

import json
import os
from typing import Any

import psycopg

from .config import (
    JUDGE_VERSION,
    SLASH_RATE_DEN,
    SLASH_RATE_NUM,
)
from .crypto import (
    canonical_evidence_blob,
    content_hash,
    evidence_id as compute_evidence_id,
    sha256,
    snapshot_canonical,
    vote_signed_bytes,
)
from .errors import Conflict, InvalidVote, NotFound


# ---------------------------------------------------------------- 基础数据


def epoch_of(round: int, epoch_length: int) -> int:
    return round // epoch_length


def create_chain(conn, chain_id: str, epoch_length: int) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO chains (chain_id, epoch_length) VALUES (%s, %s) "
            "ON CONFLICT (chain_id) DO NOTHING",
            (chain_id, epoch_length),
        )
        cur.execute("SELECT * FROM chains WHERE chain_id = %s", (chain_id,))
        row = cur.fetchone()
    return dict(row)


def register_validator(conn, chain_id: str, pubkey: bytes, moniker: str = "") -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM chains WHERE chain_id = %s", (chain_id,))
        if cur.fetchone() is None:
            raise NotFound(f"链 {chain_id} 不存在")
        cur.execute(
            "INSERT INTO validators (chain_id, validator_pubkey, moniker) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (chain_id, pubkey, moniker),
        )
        cur.execute(
            """
            INSERT INTO validator_power (chain_id, validator_pubkey, power)
            VALUES (%s, %s, 0)
            ON CONFLICT (chain_id, validator_pubkey) DO NOTHING
            """,
            (chain_id, pubkey),
        )


def set_power(conn, chain_id: str, pubkey: bytes, power: int) -> int:
    """设置当前权益（委托/解绑都走这里）。绝不触碰历史快照。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM validators WHERE chain_id = %s AND validator_pubkey = %s",
            (chain_id, pubkey),
        )
        if cur.fetchone() is None:
            raise NotFound("验证者不存在")
        cur.execute(
            """
            INSERT INTO validator_power (chain_id, validator_pubkey, power, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (chain_id, validator_pubkey)
            DO UPDATE SET power = EXCLUDED.power, updated_at = now()
            """,
            (chain_id, pubkey, power),
        )
    return power


def freeze_epoch(conn, chain_id: str, epoch: int) -> dict[str, Any]:
    """在 epoch 边界冻结权益快照。快照不可变；重复冻结同一 epoch 报错。"""
    with conn.cursor() as cur:
        cur.execute("SELECT epoch_length FROM chains WHERE chain_id = %s", (chain_id,))
        row = cur.fetchone()
        if row is None:
            raise NotFound(f"链 {chain_id} 不存在")

        cur.execute("SELECT 1 FROM stake_snapshots WHERE chain_id = %s AND epoch = %s",
                    (chain_id, epoch))
        if cur.fetchone() is not None:
            raise Conflict(f"链 {chain_id} epoch {epoch} 的快照已冻结")

        cur.execute(
            """
            SELECT vp.validator_pubkey, vp.power
            FROM validator_power vp
            WHERE vp.chain_id = %s AND vp.power > 0
            ORDER BY vp.validator_pubkey
            """,
            (chain_id,),
        )
        entries = [(r["validator_pubkey"], r["power"]) for r in cur.fetchall()]
        if not entries:
            raise Conflict("没有任何正权益验证者，拒绝冻结空快照")

        snap_hash = sha256(snapshot_canonical(entries))
        total = sum(p for _, p in entries)

        cur.execute(
            """
            INSERT INTO stake_snapshots (chain_id, epoch, snapshot_hash, total_power)
            VALUES (%s, %s, %s, %s)
            """,
            (chain_id, epoch, snap_hash, total),
        )
        cur.executemany(
            """
            INSERT INTO stake_snapshot_entries (chain_id, epoch, validator_pubkey, power)
            VALUES (%s, %s, %s, %s)
            """,
            [(chain_id, epoch, pk, p) for pk, p in entries],
        )
        return {
            "chain_id": chain_id,
            "epoch": epoch,
            "total_power": total,
            "validator_count": len(entries),
            "snapshot_hash": snap_hash.hex(),
        }


# ---------------------------------------------------------------- 证据归并


def ingest_vote(
    conn,
    *,
    chain_id: str,
    pubkey: bytes,
    round: int,
    block_hash: bytes,
    signature: bytes,
    raw: dict[str, Any],
) -> dict[str, Any]:
    """归并一张离线投票（调用前必须已完成签名验证）。

    返回 {"result": ...}，result 取值：
      duplicate        相同投票重复输入（不处罚）
      first            该 (验证者,轮次) 的第一张票
      already_evidence 迟到票，证据此前已存在（不再处罚）
      new_evidence     与先到的票冲突，本次归并出新证据（调用方提交后处罚）
    new_evidence 时附带 evidence_id / epoch / 两张原始票。
    """
    chash = content_hash(
        chain_id=chain_id, validator_pubkey=pubkey, round=round, block_hash=block_hash
    )
    signed = vote_signed_bytes(
        chain_id=chain_id, validator_pubkey=pubkey, round=round, block_hash=block_hash
    )

    with conn.cursor() as cur:
        # 串行化同一验证者的归并：杜绝两张冲突票并发时双双「看不到对方」。
        cur.execute("SELECT epoch_length FROM chains WHERE chain_id = %s", (chain_id,))
        chain = cur.fetchone()
        if chain is None:
            raise NotFound(f"链 {chain_id} 不存在")
        cur.execute(
            "SELECT 1 FROM validators WHERE chain_id = %s AND validator_pubkey = %s",
            (chain_id, pubkey),
        )
        if cur.fetchone() is None:
            raise InvalidVote(
                "验证者公钥未在该链注册（跨链投票或未知验证者一律拒绝）",
                code="validator_not_registered",
            )
        epoch_length = chain["epoch_length"]
        epoch = epoch_of(round, epoch_length)

        # 行锁（注册行一定存在），保证同验证者的票串行归并。
        cur.execute(
            "SELECT moniker FROM validators WHERE chain_id = %s AND validator_pubkey = %s "
            "FOR UPDATE",
            (chain_id, pubkey),
        )
        cur.fetchone()

        # 1) 重复相同投票 -> 幂等丢弃。
        cur.execute(
            "SELECT evidence_id FROM votes WHERE chain_id = %s AND signature = %s",
            (chain_id, signature),
        )
        sig_row = cur.fetchone()
        if sig_row is not None:
            return {"result": "duplicate", "reason": "same_signature"}
        cur.execute(
            "SELECT evidence_id FROM votes "
            "WHERE chain_id = %s AND validator_pubkey = %s AND round = %s AND content_hash = %s",
            (chain_id, pubkey, round, chash),
        )
        if cur.fetchone() is not None:
            return {"result": "duplicate", "reason": "same_content"}

        # 2) 该 (链,验证者,轮次) 已有证据 -> 迟到票，只归档，不再处罚。
        cur.execute(
            "SELECT evidence_id, status FROM evidences "
            "WHERE chain_id = %s AND validator_pubkey = %s AND round = %s",
            (chain_id, pubkey, round),
        )
        existing_ev = cur.fetchone()
        if existing_ev is not None:
            cur.execute(
                """
                INSERT INTO votes (chain_id, validator_pubkey, round, block_hash,
                                   signature, content_hash, raw_vote, evidence_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                ON CONFLICT (chain_id, signature) DO NOTHING
                """,
                (chain_id, pubkey, round, block_hash, signature, chash,
                 json.dumps(raw, separators=(",", ":"), sort_keys=True),
                 existing_ev["evidence_id"]),
            )
            return {
                "result": "already_evidence",
                "evidence_id": existing_ev["evidence_id"].hex(),
                "evidence_status": existing_ev["status"],
            }

        # 3) 已有不同内容的先到票且尚无证据 -> 双签，归并出新证据。
        cur.execute(
            """
            SELECT block_hash, signature, content_hash, raw_vote
            FROM votes
            WHERE chain_id = %s AND validator_pubkey = %s AND round = %s
            ORDER BY received_at, signature
            """,
            (chain_id, pubkey, round),
        )
        prior_rows = cur.fetchall()

        if prior_rows:
            prior = prior_rows[0]
            prior_signed = vote_signed_bytes(
                chain_id=chain_id,
                validator_pubkey=pubkey,
                round=round,
                block_hash=prior["block_hash"],
            )
            blob = canonical_evidence_blob([prior_signed, signed])
            eid = compute_evidence_id([prior_signed, signed])
            raw_evidence = [prior["raw_vote"], raw]

            cur.execute(
                """
                INSERT INTO evidences (evidence_id, chain_id, validator_pubkey, round,
                                       epoch, raw_evidence, canonical_blob, judge_version,
                                       status)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, 'pending')
                ON CONFLICT (chain_id, validator_pubkey, round) DO NOTHING
                """,
                (eid, chain_id, pubkey, round, epoch,
                 json.dumps(raw_evidence, separators=(",", ":"), sort_keys=True),
                 blob, JUDGE_VERSION),
            )
            if cur.rowcount == 0:
                # 并发极端情况：被别的事务抢先建证据。按「已有证据」处理。
                cur.execute(
                    "SELECT evidence_id, status FROM evidences "
                    "WHERE chain_id = %s AND validator_pubkey = %s AND round = %s",
                    (chain_id, pubkey, round),
                )
                won = cur.fetchone()
                cur.execute(
                    """
                    INSERT INTO votes (chain_id, validator_pubkey, round, block_hash,
                                       signature, content_hash, raw_vote, evidence_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (chain_id, signature) DO NOTHING
                    """,
                    (chain_id, pubkey, round, block_hash, signature, chash,
                     json.dumps(raw, separators=(",", ":"), sort_keys=True),
                     won["evidence_id"]),
                )
                return {
                    "result": "already_evidence",
                    "evidence_id": won["evidence_id"].hex(),
                    "evidence_status": won["status"],
                }

            # 回填先到票与本次票的证据归属。
            cur.execute(
                "UPDATE votes SET evidence_id = %s "
                "WHERE chain_id = %s AND validator_pubkey = %s AND round = %s "
                "AND signature = %s",
                (eid, chain_id, pubkey, round, prior["signature"]),
            )
            cur.execute(
                """
                INSERT INTO votes (chain_id, validator_pubkey, round, block_hash,
                                   signature, content_hash, raw_vote, evidence_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (chain_id, pubkey, round, block_hash, signature, chash,
                 json.dumps(raw, separators=(",", ":"), sort_keys=True), eid),
            )
            return {
                "result": "new_evidence",
                "evidence_id": eid.hex(),
                "epoch": epoch,
            }

        # 4) 第一张票。
        cur.execute(
            """
            INSERT INTO votes (chain_id, validator_pubkey, round, block_hash,
                               signature, content_hash, raw_vote)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (chain_id, pubkey, round, block_hash, signature, chash,
             json.dumps(raw, separators=(",", ":"), sort_keys=True)),
        )
        return {"result": "first", "epoch": epoch}


# ---------------------------------------------------------------- 惩罚


def apply_penalty(conn, *, evidence_id: bytes) -> dict[str, Any]:
    """对一份证据幂等地应用惩罚。已处罚则直接返回既有惩罚。

    证据先于惩罚提交：若本函数因崩溃未执行，recover_pending() 会补偿。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM penalties WHERE evidence_id = %s", (evidence_id,))
        existing = cur.fetchone()
        if existing is not None:
            return {"already_applied": True, "penalty": dict(existing)}

        cur.execute("SELECT * FROM evidences WHERE evidence_id = %s", (evidence_id,))
        ev = cur.fetchone()
        if ev is None:
            raise NotFound("证据不存在")
        if ev["status"] == "punished":
            # 证据标记已罚但惩罚行缺失属于异常，保守报错等待人工/恢复。
            raise Conflict("证据已标记 punished 但缺少惩罚行")

        epoch = ev["epoch"]
        cur.execute(
            """
            SELECT snapshot_hash, total_power
            FROM stake_snapshots WHERE chain_id = %s AND epoch = %s
            """,
            (ev["chain_id"], epoch),
        )
        snap = cur.fetchone()
        if snap is None:
            # 快照尚未冻结：保持 pending，等冻结后恢复处理。处罚绝不能用「当前权益」。
            return {"deferred": True, "reason": "snapshot_not_frozen", "epoch": epoch}

        cur.execute(
            """
            SELECT power FROM stake_snapshot_entries
            WHERE chain_id = %s AND epoch = %s AND validator_pubkey = %s
            """,
            (ev["chain_id"], epoch, ev["validator_pubkey"]),
        )
        entry = cur.fetchone()
        base_power = entry["power"] if entry is not None else 0
        slashed = base_power * SLASH_RATE_NUM // SLASH_RATE_DEN

        cur.execute(
            """
            INSERT INTO penalties (evidence_id, chain_id, epoch, validator_pubkey,
                                   snapshot_chain_id, snapshot_epoch, base_power,
                                   slashed_power, judge_version, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'applied')
            ON CONFLICT (evidence_id) DO NOTHING
            """,
            (evidence_id, ev["chain_id"], epoch, ev["validator_pubkey"],
             ev["chain_id"], epoch, base_power, slashed, ev["judge_version"]),
        )

        # 当前权益表也同步扣减（快照不变；验证者若已解绑，扣减不低于 0）。
        cur.execute(
            """
            UPDATE validator_power
            SET power = GREATEST(power - %s, 0), updated_at = now()
            WHERE chain_id = %s AND validator_pubkey = %s
            """,
            (slashed, ev["chain_id"], ev["validator_pubkey"]),
        )
        cur.execute(
            "UPDATE evidences SET status = 'punished' WHERE evidence_id = %s",
            (evidence_id,),
        )
        return {
            "already_applied": False,
            "deferred": False,
            "base_power": base_power,
            "slashed_power": slashed,
            "snapshot_epoch": epoch,
            "snapshot_hash": snap["snapshot_hash"].hex(),
            "judge_version": ev["judge_version"],
        }


def recover_pending(conn) -> dict[str, Any]:
    """扫描所有 pending 证据：快照就绪的补罚；未就绪的继续等待。幂等。"""
    punished, deferred = [], []
    with conn.cursor() as cur:
        cur.execute("SELECT evidence_id FROM evidences WHERE status = 'pending' ORDER BY first_seen_at")
        pending = [r["evidence_id"] for r in cur.fetchall()]
    for eid in pending:
        out = apply_penalty(conn, evidence_id=eid)
        if out.get("deferred"):
            deferred.append(eid.hex())
        else:
            punished.append(eid.hex())
    return {"recovered": punished, "still_deferred": deferred}


def crash_if_injected() -> None:
    """故障注入：证据已提交、惩罚尚未应用时硬退出（模拟真实进程崩溃）。"""
    if os.environ.get("SLASHER_CRASH_AFTER_EVIDENCE") == "1":
        os._exit(27)
