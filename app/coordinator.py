"""只读双侧协调逻辑。

协调器**只读取**两条链的状态，给出可操作建议；它不保管私钥、不发送任何
交易，因此不可能"替用户完成"跨链原子交换。它能做的是把 HTLC 原语的
风险边界显式呈现出来（见 assess() 的建议规则与 README）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .chain import LegClient, SwapView

# 时间约定（与 README 一致）：
#   tB（Beta 时间锁，领取首次泄露 preimage 的一侧）必须早于 tA。
#   Δ = tA − tB，至少覆盖"在 Beta 看到 claim → 在 Alpha 提交 claim"的最坏耗时。
DEFAULT_MIN_DELTA_SECONDS = 15


@dataclass
class Assessment:
    swap_id: str
    legs: dict[str, dict[str, Any]]
    status: str
    advice: list[str] = field(default_factory=list)
    time_plan: dict[str, Any] = field(default_factory=dict)
    risk_flags: list[str] = field(default_factory=list)


def _leg_payload(v: SwapView) -> dict[str, Any]:
    return {
        "leg": v.leg,
        "rpc_url": v.rpc_url,
        "reachable": v.reachable,
        "exists": v.exists,
        "state": v.state,
        "sender": v.sender,
        "receiver": v.receiver,
        "amount_wei": v.amount_wei,
        "hash_lock": "0x" + v.hash_lock if v.hash_lock else None,
        "timelock": v.timelock,
        "now_ts": v.now_ts,
        "seconds_until_timelock": v.seconds_until_timelock,
        "preimage": ("0x" + v.preimage) if v.preimage else None,
    }


def assess(alpha: LegClient, beta: LegClient, swap_id: bytes,
           min_delta: int = DEFAULT_MIN_DELTA_SECONDS) -> Assessment:
    va = alpha.get_swap(swap_id)
    vb = beta.get_swap(swap_id)
    a, b = _leg_payload(va), _leg_payload(vb)

    advices: list[str] = []
    flags: list[str] = []
    status = "UNKNOWN"

    reachable_a, reachable_b = a["reachable"], b["reachable"]
    st_a, st_b = a["state"], b["state"]

    # ---------- 时间计划检查（只在两侧都可读且时间锁存在时） ----------
    if reachable_a and reachable_b and a["timelock"] and b["timelock"]:
        t_a, t_b = a["timelock"], b["timelock"]
        delta = t_a - t_b
        time_plan = {
            "convention": "tB < tA：Beta 先到期（领取在此侧首次公开原像），Alpha 后到期",
            "tB_beta": t_b,
            "tA_alpha": t_a,
            "delta_seconds": delta,
            "min_delta_seconds": min_delta,
            "deadline_order_ok": t_b < t_a,
            "buffer_ok": delta >= min_delta,
        }
        if t_b >= t_a:
            flags.append(
                "时间锁顺序错误：tB >= tA。Beta 不会先到期，Alpha 可能先退款而"
                "Beta 一侧的 preimage 仍未公开，存在资金被困/单边结算风险。"
            )
        elif delta < min_delta:
            flags.append(
                f"安全裕度不足：Δ=tA−tB={delta}s < 建议的 {min_delta}s，"
                "若 Beta claim 到 Alpha claim 的传播稍慢，Alpha 可能先到退款截止。"
            )
    else:
        time_plan = {
            "convention": "tB < tA（至少一侧不可达或缺时间锁，无法校验）",
            "deadline_order_ok": None,
            "buffer_ok": None,
        }

    # ---------- 连通性 ----------
    if not reachable_a or not reachable_b:
        down = [leg for leg, ok in (("alpha", reachable_a), ("beta", reachable_b)) if not ok]
        status = "CHAIN_UNREACHABLE"
        flags.append(f"链不可达：{', '.join(down)}（可能正暂停/宕机）。协调器只读，无法代发交易。")
        if st_a == "Locked" or st_b == "Locked":
            flags.append(
                "暂停期间时间锁不会因你而停：若对端在自己链上领取后你的链仍不可达，"
                "你可能无法在截止前领取；若对端未领取，恢复后你可在自己截止后退款。"
            )
        advices.append("恢复该链 RPC 后重新评估；在两侧都可确认前，不要锁定任何新资金。")

    # ---------- 都可达时的状态评估 ----------
    elif not a["exists"] and not b["exists"]:
        status = "ABSENT_BOTH"
        advices.append("两侧都没有该 swapId 的锁定记录。")

    elif a["exists"] ^ b["exists"]:
        locked_leg = "alpha" if a["exists"] else "beta"
        other = "beta" if locked_leg == "alpha" else "alpha"
        status = "ONE_SIDED_LOCK"
        flags.append(
            f"只有 {locked_leg} 一侧已锁定，{other} 尚未锁定。"
            "先锁方面临对手方永不锁币的风险（只能等自己的时间锁到期退款）。"
        )
        advices.append(
            f"在 {other} 侧确认对等锁定（相同 hashLock、约定的时间锁顺序）"
            "之前，不要把任何可利用的信息交给对手方。"
        )

    elif a["hash_lock"] != b["hash_lock"]:
        status = "HASH_MISMATCH"
        flags.append("两侧 hashLock 不同：不可能用同一个 preimage 同时领取，禁止继续。")

    else:
        # 两侧都存在且 hashLock 一致
        if st_a == "Locked" and st_b == "Locked":
            status = "LOCKED_BOTH"
            advices.append(
                "两侧均锁定、hashLock 一致。受益人可在任一侧凭 preimage 领取；"
                "首笔 claim 会在链上公开 preimage，另一受益人据此在自己一侧领取。"
            )
            if a["seconds_until_timelock"] is not None and b["seconds_until_timelock"] is not None:
                advices.append(
                    f"Beta 距退款截止约 {b['seconds_until_timelock']}s，"
                    f"Alpha 约 {a['seconds_until_timelock']}s；"
                    "任一侧过截止后，付款方都可以对该侧发起退款。"
                )

        elif st_b == "Claimed" and st_a == "Locked":
            status = "BETA_CLAIMED_ALPHA_PENDING"
            advices.append(
                "Beta 已被领取，preimage 已在 Beta 链上公开（见 beta.preimage）。"
                "Alpha 受益人应立即用该 preimage 在 Alpha 领取——"
                f"Alpha 距截止约 {a['seconds_until_timelock']}s。"
            )
            flags.append(
                "若 Alpha 在截止前持续不可用或受益人不作为，Alpha 付款方到期可退款，"
                "交换将变为 Beta 已结算、Alpha 退款的非原子结果。"
            )

        elif st_a == "Claimed" and st_b == "Locked":
            status = "ALPHA_CLAIMED_BETA_PENDING"
            advices.append(
                "Alpha 已被领取。按 tB<tA 约定 Beta 应更早到期——请核对谁先领取；"
                "Beta 仍 Locked 时，受益人可凭同一 preimage 立即在 Beta 领取。"
            )

        elif st_a == "Claimed" and st_b == "Claimed":
            pre_ok = a["preimage"] == b["preimage"] and a["preimage"] is not None
            status = "CLAIMED_BOTH"
            advices.append("两侧都已领取，交换完成。" if pre_ok
                           else "两侧都已 Claimed 但事件里的 preimage 不一致，请人工核查。")

        elif st_a == "Refunded" and st_b == "Refunded":
            status = "REFUNDED_BOTH"
            advices.append("两侧均已超时退款，交换取消，资金各自回到付款方。")

        elif st_a == "Refunded" and st_b == "Locked":
            status = "ALPHA_REFUNDED_BETA_LOCKED"
            flags.append(
                "Alpha 已退款而 Beta 仍锁定。Beta 受益人此时领取等于白送："
                "付出 Beta 资金却无法再领取 Alpha。"
            )
            advices.append("Beta 付款方等待 Beta 时间锁到期后退款即可；受益人不要再领取。")

        elif st_b == "Refunded" and st_a == "Locked":
            status = "BETA_REFUNDED_ALPHA_LOCKED"
            flags.append(
                "Beta 已退款而 Alpha 仍锁定。Alpha 受益人此时领取等于白送："
                "付出 Alpha 资金却无法再领取 Beta。"
            )
            advices.append("Alpha 付款方等待 Alpha 时间锁到期后退款即可；受益人不要再领取。")

        elif st_a == "Refunded" and st_b == "Claimed":
            status = "SPLIT_BETA_CLAIMED_ALPHA_REFUNDED"
            flags.append(
                "非原子结果已经发生：Beta 被领取、Alpha 被退款。"
                "HTLC 原语本身无法回滚这种局面——这正是 tB<tA 与执行裕度要规避的情形。"
            )

        elif st_b == "Refunded" and st_a == "Claimed":
            status = "SPLIT_ALPHA_CLAIMED_BETA_REFUNDED"
            flags.append("非原子结果已经发生：Alpha 被领取、Beta 被退款。请人工核对时序。")

        else:
            status = "OTHER"
            flags.append(f"未覆盖的状态组合：alpha={st_a} beta={st_b}")

    return Assessment(
        swap_id="0x" + swap_id.hex(),
        legs={"alpha": a, "beta": b},
        status=status,
        advice=advices,
        time_plan=time_plan,
        risk_flags=flags,
    )
