#!/usr/bin/env python3
"""端到端演示（对接 scripts/anvil-start.sh 启动的两条 anvil 链）。

场景：
  happy      两侧锁定 → Beta 凭原像领取（公开 preimage）→ Alpha 凭同一原像领取
  timeout    短时间锁：截止前退款被拒 → 到点退款 → 另一侧退款
  badclaim   错误原像被拒、重复领取/退款被拒
  pause      Alpha 链 SIGSTOP 暂停 → 协调器标记不可达与风险 → 恢复后状态仍在

用法（先 anvil-start.sh + deploy.py）：
    .venv/bin/python scripts/demo.py happy
    .venv/bin/python scripts/demo.py timeout
    .venv/bin/python scripts/demo.py badclaim
    .venv/bin/python scripts/demo.py pause
    .venv/bin/python scripts/demo.py all
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.chain import (  # noqa: E402
    LegClient, SwapView, load_abi, make_hash_lock, make_swap_id, packed_preimage,
)
from app.config import load_manifest, role_keys  # noqa: E402
from app.coordinator import assess  # noqa: E402

PREIMAGE = packed_preimage("0x" + "ab" * 32)
HASH_LOCK = make_hash_lock(PREIMAGE)
RUN_DIR = Path(".run")


def banner(t: str) -> None:
    print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


def clients() -> tuple[LegClient, LegClient]:
    cfgs = load_manifest()
    abi = load_abi()
    return (
        LegClient("alpha", cfgs["alpha"].rpc_url, cfgs["alpha"].chain_id,
                  cfgs["alpha"].address, cfgs["alpha"].deploy_block, abi),
        LegClient("beta", cfgs["beta"].rpc_url, cfgs["beta"].chain_id,
                  cfgs["beta"].address, cfgs["beta"].deploy_block, abi),
    )


def show(alpha: LegClient, beta: LegClient, sid: bytes, note: str) -> None:
    r = assess(alpha, beta, sid)
    print(f"\n--- {note} ---")
    print(f"status={r.status}")
    print(f"  alpha: {r.legs['alpha']['state']}  timelock={r.legs['alpha']['timelock']}  "
          f"剩余 {r.legs['alpha']['seconds_until_timelock']}s")
    print(f"  beta : {r.legs['beta']['state']}  timelock={r.legs['beta']['timelock']}  "
          f"剩余 {r.legs['beta']['seconds_until_timelock']}s")
    for f in r.risk_flags:
        print(f"  ⚠ 风险: {f}")
    for a in r.advice:
        print(f"  → 建议: {a}")
    tp = r.time_plan
    if tp.get("deadline_order_ok") is not None:
        print(f"  时间约定 tB<tA: {tp['deadline_order_ok']}  Δ={tp['delta_seconds']}s "
              f"(≥{tp['min_delta_seconds']}s 视为有裕度: {tp['buffer_ok']})")


def lock_both(alpha: LegClient, beta: LegClient, seed: str,
              beta_ttl: int, delta: int, amount: int = 10**15) -> bytes:
    sid = make_swap_id(seed)
    keys = role_keys()
    now = max(alpha.now(), beta.now()) + 1
    t_b = now + beta_ttl
    t_a = t_b + delta
    alpha.lock(keys["alpha"]["sender"], sid,
               alpha.account(keys["alpha"]["receiver"]).address,
               HASH_LOCK, t_a, amount)
    beta.lock(keys["beta"]["sender"], sid,
              beta.account(keys["beta"]["receiver"]).address,
              HASH_LOCK, t_b, amount)
    print(f"锁定完成 swapId=0x{sid.hex()[:16]}…  tB={t_b} tA={t_a} Δ={delta}s")
    return sid


def scenario_happy() -> None:
    banner("场景 1：正常双侧领取（tB<tA，Δ=30s）")
    alpha, beta = clients()
    sid = lock_both(alpha, beta, f"happy-{time.time()}", beta_ttl=300, delta=30)
    show(alpha, beta, sid, "双侧锁定后")

    print("\n[Beta 受益人凭原像领取 → preimage 首次在 Beta 链上公开]")
    beta.claim(role_keys()["beta"]["receiver"], sid, PREIMAGE)
    show(alpha, beta, sid, "Beta 已领取，Alpha 待领取")

    print("\n[Alpha 受益人用 Beta 事件里的同一个 preimage 领取]")
    view_b: SwapView = beta.get_swap(sid)
    alpha.claim(role_keys()["alpha"]["receiver"], sid, packed_preimage("0x" + view_b.preimage))
    show(alpha, beta, sid, "最终状态：双侧 Claimed")


def scenario_timeout() -> None:
    banner("场景 2：超时退款（截止前被拒、到点可退、两侧互不影响）")
    alpha, beta = clients()
    sid = lock_both(alpha, beta, f"timeout-{time.time()}", beta_ttl=4, delta=6)
    show(alpha, beta, sid, "刚锁定")

    try:
        print("\n[在截止时刻之前尝试 Beta 退款 —— 预期被合约拒绝]")
        beta.refund(role_keys()["beta"]["sender"], sid)
    except RuntimeError as e:
        print(f"  被拒绝（符合预期）: {e}")

    print("  等待 Beta 时间锁到期（约 5 秒）…")
    time.sleep(6)
    beta.refund(role_keys()["beta"]["sender"], sid)
    show(alpha, beta, sid, "Beta 已退款；Alpha 仍 Locked")

    print("\n[此时 Alpha 受益人若领取 = 白送；等 Alpha 截止后付款方退款]")
    time.sleep(7)
    alpha.refund(role_keys()["alpha"]["sender"], sid)
    show(alpha, beta, sid, "最终状态：双侧 Refunded")


def scenario_badclaim() -> None:
    banner("场景 3：错误原像 / 重复领取 / 终态后退款 全部被拒")
    alpha, beta = clients()
    sid = lock_both(alpha, beta, f"bad-{time.time()}", beta_ttl=300, delta=30)
    keys = role_keys()

    wrong = packed_preimage("0x" + "cd" * 32)
    for desc, fn in [
        ("错误原像", lambda: beta.claim(keys["beta"]["receiver"], sid, wrong)),
        ("非受益人提交正确原像", lambda: beta.claim(keys["beta"]["sender"], sid, PREIMAGE)),
    ]:
        try:
            fn()
            print(f"  {desc}: 居然成功了（异常！）")
        except RuntimeError as e:
            print(f"  {desc}: 被拒绝（符合预期）: {str(e)[:160]}")

    beta.claim(keys["beta"]["receiver"], sid, PREIMAGE)
    for desc, fn in [
        ("重复领取", lambda: beta.claim(keys["beta"]["receiver"], sid, PREIMAGE)),
        ("领取后再退款（即便等到截止后逻辑也关闭）", lambda: beta.refund(keys["beta"]["sender"], sid)),
    ]:
        try:
            fn()
            print(f"  {desc}: 居然成功了（异常！）")
        except RuntimeError as e:
            print(f"  {desc}: 被拒绝（符合预期）: {str(e)[:160]}")
    show(alpha, beta, sid, "Beta Claimed 终态不可变；Alpha 仍可在截止前领取或到期退款")


def _pid(leg: str) -> int | None:
    f = RUN_DIR / f"{leg}.pid"
    return int(f.read_text().strip()) if f.exists() else None


def scenario_pause() -> None:
    banner("场景 4：一链暂停（SIGSTOP 模拟停机/网络分区）——风险边界展示")
    alpha, beta = clients()
    sid = lock_both(alpha, beta, f"pause-{time.time()}", beta_ttl=300, delta=30)
    pid = _pid("alpha")
    if pid is None:
        print("找不到 .run/alpha.pid，请用 scripts/anvil-start.sh 启动链后再演示本场景。")
        return

    print(f"\n[SIGSTOP 暂停 Alpha anvil (pid={pid})；Beta 链继续运行]")
    os.kill(pid, signal.SIGSTOP)
    try:
        time.sleep(1)
        show(alpha, beta, sid, "Alpha 暂停期间评估")
        print("\n  演示要点：协调器是只读的，停在这一步什么都无法替你完成；")
        print("  时间锁按各自链上时间继续逼近。若 Beta 此时 claim，你将无法在")
        print("  Alpha 停机期间领取 Alpha；恢复后若 Alpha 已过截止，付款方可退款，")
        print("  最终可能是 Beta Claimed + Alpha Refunded 的非原子结局。")
    finally:
        print(f"\n[SIGCONT 恢复 Alpha anvil (pid={pid})]")
        os.kill(pid, signal.SIGCONT)
        time.sleep(1)

    show(alpha, beta, sid, "恢复后：锁定状态原样保留")
    beta.claim(role_keys()["beta"]["receiver"], sid, PREIMAGE)
    print("[Beta 已领取；恢复后的 Alpha 仍可在自己截止前凭 preimage 领取]")
    alpha.claim(role_keys()["alpha"]["receiver"], sid, PREIMAGE)
    show(alpha, beta, sid, "恢复后完成双侧领取（本次结局良好，但靠的是 Δ 裕度与快速行动）")


SCENARIOS = {
    "happy": scenario_happy,
    "timeout": scenario_timeout,
    "badclaim": scenario_badclaim,
    "pause": scenario_pause,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=[*SCENARIOS.keys(), "all"])
    args = ap.parse_args()
    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    for n in names:
        SCENARIOS[n]()
    print("\n演示结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
