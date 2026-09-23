"""终局检测服务核心：投票处理、双投证据、严格超多数判定、冲突冻结、重放重建。

规则（与 README 一致）：
- 验证者集合按 epoch 固定；不同 epoch 的投票绝不混算。
- 同一验证者对同一块重复投票只计一次（重复票幂等拒绝，不重复计权）。
- 同一验证者对不同块投票 = 双投：两条票全部留档为证据，其权重从有效
  计票中排除（明确排除规则）；表观计票保留全部投票，用于观察冲突。
- 有效计票严格超过总权重 2/3（整数比较 3w > 2W）才终局；W 含零权重验证者。
- 终局检查点按投票到达顺序首次达成即固化；之后任何消息都不能覆盖或回滚。
- 若另一个块在表观计票（含双投者的票）中同样超过 2/3，说明出现了第二个
  2/3 利益集合（安全故障，只能由双投造成）：记录全部证据、告警、冻结推进。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from . import crypto
from .finality import is_strict_supermajority, required_weight
from .storage import Database


class VoteRejected(Exception):
    """投票被拒绝。status 为建议的 HTTP 状态码。"""

    def __init__(self, reason: str, status: int = 422):
        super().__init__(reason)
        self.reason = reason
        self.status = status


@dataclass
class _Vote:
    id: int
    validator: str
    block_hash: str
    signature: str


@dataclass
class _Replay:
    votes: list[_Vote]
    equivocators: dict[str, list[str]]  # validator -> [block_hash,...]（保留出现顺序）
    equivocation_records: list[dict[str, Any]]
    effective: dict[str, dict[str, Any]]  # block -> {weight, validators}
    apparent: dict[str, dict[str, Any]]
    finalized_block: Optional[str]
    finalized_weight: Optional[int]
    conflict: Optional[dict[str, Any]]
    state: str


def _add_weight(tally: dict[str, dict[str, Any]], v: _Vote, weight: int) -> None:
    b = tally.setdefault(v.block_hash, {"weight": 0, "validators": []})
    if v.validator not in b["validators"]:
        b["weight"] += weight
        b["validators"].append(v.validator)


def _sorted_tally(tally: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [
        {"block_hash": k, "weight": v["weight"], "validators": sorted(v["validators"])}
        for k, v in tally.items()
    ]
    rows.sort(key=lambda r: (-r["weight"], r["block_hash"]))
    return rows


def _replay(
    conn: sqlite3.Connection,
    epoch: int,
    validators: dict[str, sqlite3.Row],
    total_weight: int,
) -> _Replay:
    """对一个 epoch 的全部已落库投票按到达顺序做确定性增量重放。

    语义（与 README 一致）：
    - 双投在“第二条不同块的票到达时”被发现：该验证者的权重从有效计票中
      追溯移除（两条票都留档为证据）；表观计票始终包含全部投票。
    - 终局检查点在有效计票首次越线时固化，之后任何消息（包括新发现的双投）
      都不能回滚或覆盖它。
    - 冲突：另一个块在表观计票中同样越过 2/3，即出现第二个 2/3 利益集合。

    这是唯一的事实计算函数；启动时与持久化的 checkpoint/conflict 核对，
    从而保证“重启可重建同一结论”。
    """
    rows = conn.execute(
        "SELECT id, validator, block_hash, signature FROM votes "
        "WHERE epoch=? ORDER BY id",
        (epoch,),
    ).fetchall()
    votes = [
        _Vote(r["id"], r["validator"], r["block_hash"], r["signature"]) for r in rows
    ]

    per_validator: dict[str, list[str]] = {}  # 每个验证者已见到的不同块（保序）
    equivocators: dict[str, list[str]] = {}   # 已发现的双投者
    effective: dict[str, dict[str, Any]] = {}
    apparent: dict[str, dict[str, Any]] = {}
    finalized_block: Optional[str] = None
    finalized_weight: Optional[int] = None
    conflict: Optional[dict[str, Any]] = None

    # 按到达顺序增量处理；检查点在“首次达成”的时刻固化，之后不被任何消息覆盖
    for v in votes:
        w = int(validators[v.validator]["weight"])
        blocks = per_validator.setdefault(v.validator, [])
        if v.block_hash not in blocks:
            blocks.append(v.block_hash)
            if len(blocks) == 2 and v.validator not in equivocators:
                # 发现双投：从有效计票中追溯移除该验证者的全部权重
                equivocators[v.validator] = list(blocks)
                for b in blocks:
                    t = effective.get(b)
                    if t and v.validator in t["validators"]:
                        t["weight"] -= w
                        t["validators"].remove(v.validator)

        _add_weight(apparent, v, w)  # 表观账本：全部投票都计权
        if v.validator not in equivocators:
            _add_weight(effective, v, w)

        if finalized_block is None:
            cross = [
                b
                for b, t in effective.items()
                if is_strict_supermajority(t["weight"], total_weight)
            ]
            if cross:
                # 数学上至多一个；防御性确定性选择：权重最高，其次块哈希最小
                cross.sort(key=lambda b: (-effective[b]["weight"], b))
                finalized_block = cross[0]
                finalized_weight = effective[finalized_block]["weight"]

        if conflict is None and finalized_block is not None:
            other = [
                b
                for b, t in apparent.items()
                if b != finalized_block
                and is_strict_supermajority(t["weight"], total_weight)
            ]
            if other:
                other.sort(key=lambda b: (-apparent[b]["weight"], b))
                bad = other[0]
                conflict = {
                    "reason": (
                        "检测到终局分歧：两个不同块各自获得严格超过 2/3 总权重的"
                        "投票（第二个 2/3 利益集合只能由双投产生），违反安全性。"
                        "epoch 已冻结，终局检查点不回滚。"
                    ),
                    "finalized_block": finalized_block,
                    "conflicting_block": bad,
                    "finalized_weight": finalized_weight,
                    "conflicting_apparent_weight": apparent[bad]["weight"],
                    "equivocators": sorted(equivocators.keys()),
                    "votes_for": {
                        finalized_block: sorted(
                            x.id for x in votes if x.block_hash == finalized_block
                        ),
                        bad: sorted(x.id for x in votes if x.block_hash == bad),
                    },
                }

    if conflict is not None:
        state = "frozen_conflict"
    elif finalized_block is not None:
        state = "finalized"
    else:
        state = "pending"

    equiv_records = [
        {
            "epoch": epoch,
            "validator_id": val,
            "weight": int(validators[val]["weight"]),
            "block_hashes": list(blocks),
            "vote_ids": sorted(v.id for v in votes if v.validator == val),
        }
        for val, blocks in sorted(equivocators.items())
    ]

    return _Replay(
        votes=votes,
        equivocators=equivocators,
        equivocation_records=equiv_records,
        effective=effective,
        apparent=apparent,
        finalized_block=finalized_block,
        finalized_weight=finalized_weight,
        conflict=conflict,
        state=state,
    )


def _persist_replay(
    db: Database,
    conn: sqlite3.Connection,
    epoch: int,
    total_weight: int,
    res: _Replay,
) -> None:
    """把重放派生态写回（调用方已持有事务）。checkpoint 只增不改块。"""
    db.replace_equivocations(conn, epoch, res.equivocation_records)
    if res.finalized_block is not None:
        db.upsert_checkpoint(
            conn,
            epoch,
            {
                "block_hash": res.finalized_block,
                "weight": res.finalized_weight,
                "required_weight": required_weight(total_weight),
                "total_weight": total_weight,
            },
        )
    if res.conflict is not None:
        db.upsert_conflict(conn, epoch, res.conflict)
    db.set_epoch_state(conn, epoch, res.state)


class FinalityService:
    def __init__(self, db: Database):
        self.db = db

    def create_epoch(self, epoch: int, validators: list[dict[str, Any]]) -> dict[str, Any]:
        total = sum(int(v["weight"]) for v in validators)
        with self.db.tx() as conn:
            if self.db.epoch_exists(conn, epoch):
                raise VoteRejected(f"epoch {epoch} 已存在", status=409)
            self.db.insert_epoch(conn, epoch, total, validators)
        return {"epoch": epoch, "total_weight": total, "validators": len(validators)}

    def submit_vote(self, payload: dict[str, Any]) -> dict[str, Any]:
        epoch = int(payload["epoch"])
        validator = payload["validator_id"]
        block_hash = payload["block_hash"]
        signature = payload["signature"]

        with self.db.tx() as conn:
            epoch_row = conn.execute(
                "SELECT * FROM epochs WHERE epoch=?", (epoch,)
            ).fetchone()
            if epoch_row is None:
                raise VoteRejected(f"未知 epoch：{epoch}（不同 epoch 不能混算）", 404)
            if epoch_row["state"] == "frozen_conflict":
                raise VoteRejected(
                    f"epoch {epoch} 已因终局冲突冻结，拒绝任何推进性投票", 409
                )

            vrow = conn.execute(
                "SELECT weight, public_key FROM validators "
                "WHERE epoch=? AND validator=?",
                (epoch, validator),
            ).fetchone()
            if vrow is None:
                raise VoteRejected(
                    f"验证者 {validator} 不属于 epoch {epoch} 的固定集合"
                )

            if not crypto.verify_vote(
                vrow["public_key"], epoch, block_hash, signature
            ):
                raise VoteRejected("签名验证失败：投票消息与该验证者公钥不匹配")

            # 重复票：同人同块，只计一次，幂等拒绝
            dup = conn.execute(
                "SELECT id FROM votes WHERE epoch=? AND validator=? AND block_hash=?",
                (epoch, validator, block_hash),
            ).fetchone()
            if dup is not None:
                raise VoteRejected(
                    f"验证者 {validator} 对该块的投票已存在（vote_id={dup['id']}），"
                    "重复投票只计一次"
                )

            vote_id = self.db.insert_vote(
                conn, epoch, validator, block_hash, signature
            )

            validators = {
                r["validator"]: r
                for r in conn.execute(
                    "SELECT validator, weight, public_key FROM validators WHERE epoch=?",
                    (epoch,),
                ).fetchall()
            }
            res = _replay(conn, epoch, validators, int(epoch_row["total_weight"]))
            _persist_replay(
                self.db, conn, epoch, int(epoch_row["total_weight"]), res
            )

            return {
                "accepted": True,
                "stored": True,
                "reason": None,
                "vote_id": vote_id,
                "epoch_state": res.state,
            }


def build_status(db: Database, epoch: int) -> dict[str, Any]:
    epoch_row = db.get_epoch(epoch)
    if epoch_row is None:
        raise VoteRejected(f"未知 epoch：{epoch}", 404)
    validators = db.get_validators(epoch)
    res = _replay(db.conn, epoch, validators, int(epoch_row["total_weight"]))
    total = int(epoch_row["total_weight"])
    return {
        "epoch": epoch,
        "state": res.state,
        "total_weight": total,
        "required_weight": required_weight(total),
        "accepted_votes": len(res.votes),
        "finalized_block": res.finalized_block,
        "finalized_weight": res.finalized_weight,
        "equivocations": res.equivocation_records,
        "effective_tally": _sorted_tally(res.effective),
        "apparent_tally": _sorted_tally(res.apparent),
        "conflict": res.conflict,
    }


def reconcile(db: Database) -> list[int]:
    """启动时对每个 epoch 重放，并与持久化的 checkpoint/conflict/state 严格核对。

    不一致直接报错（拒绝在损坏/被篡改的状态上继续服务）。返回核对过的 epoch 列表。
    """
    checked: list[int] = []
    for erow in db.list_epochs():
        epoch = int(erow["epoch"])
        total = int(erow["total_weight"])
        validators = db.get_validators(epoch)
        res = _replay(db.conn, epoch, validators, total)

        cp = db.get_checkpoint(epoch)
        if res.finalized_block is None:
            if cp is not None:
                raise RuntimeError(f"epoch {epoch}：重放无终局，但库中存在 checkpoint")
        else:
            if cp is None:
                raise RuntimeError(f"epoch {epoch}：重放有终局，但库中缺少 checkpoint")
            if cp["block_hash"] != res.finalized_block:
                raise RuntimeError(
                    f"epoch {epoch}：checkpoint 分歧 库={cp['block_hash']} "
                    f"重放={res.finalized_block}"
                )
            if int(cp["weight"]) != int(res.finalized_weight):
                raise RuntimeError(f"epoch {epoch}：checkpoint 权重与重放不一致")

        cf = db.get_conflict(epoch)
        if (cf is None) != (res.conflict is None):
            raise RuntimeError(f"epoch {epoch}：conflict 记录与重放不一致")
        if erow["state"] != res.state:
            raise RuntimeError(
                f"epoch {epoch}：state 分歧 库={erow['state']} 重放={res.state}"
            )
        checked.append(epoch)
    return checked
