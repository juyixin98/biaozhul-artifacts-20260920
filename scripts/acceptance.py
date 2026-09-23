"""端到端验收脚本：对真实运行的服务（uvicorn）跑四大场景与密码协议检查。

用法：
    uvicorn app.main:app --port 8000 &      # 另开一个终端
    python scripts/acceptance.py            # 默认 http://127.0.0.1:8000

也可由 scripts/acceptance.sh 自动起停服务。
所有密码操作（Ed25519 签名/验签、SHA-256 摘要）均真实执行，无桩实现。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.client import ClientError, DrainClient  # noqa: E402
from app.crypto import parse_public_pem, verify_object_signature  # noqa: E402

BASE_URL = os.environ.get("DRAIN_URL", "http://127.0.0.1:8000")

PASS, FAIL = "✅", "❌"
results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))
    print(f"  {PASS if cond else FAIL} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        raise SystemExit(1)


def wait_healthy(c: DrainClient, timeout: float = 15) -> None:
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = c.http.get("/healthz")
            if r.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.3)
    raise SystemExit(f"{FAIL} 服务 {BASE_URL} 不可达")


def dpod(name, node, ready=True, dep="web", ns="default", labels=None, phase="Running", owner_kind="Deployment"):
    return {
        "name": name, "uid": f"uid-{name}", "namespace": ns, "node": node,
        "phase": phase, "ready": ready,
        "labels": labels or {"app": dep},
        "owner_kind": owner_kind, "owner_name": dep,
        "controller": True, "has_empty_dir": False,
    }


def dep(name="web", ns="default", replicas=3, delay=1, selector=None, template=None):
    selector = selector or {"app": name}
    return {"name": name, "namespace": ns, "replicas": replicas, "ready_replicas": replicas,
            "selector": selector, "template_labels": template or selector,
            "delay_ready_ticks": delay}


def pdb(selector=None, min_avail=None, max_unavail=None, name="pdb-web", ns="default"):
    return {"name": name, "namespace": ns, "selector": selector or {"app": "web"},
            "min_available": min_avail, "max_unavailable": max_unavail}


def snap(sid, nodes, deployments, pdbs, caps=None):
    return {
        "snapshot_id": sid,
        "nodes": [
            {"name": n, "schedulable": True, "capacity": (caps or {}).get(n, 30), "pods": pods}
            for n, pods in nodes
        ],
        "deployments": deployments, "pdbs": pdbs,
    }


def plan(c: DrainClient, s, nodes, force=False):
    return c.call("POST", "/api/plans", {"snapshot": s, "drain_nodes": nodes, "force": force})


def run(c: DrainClient, exec_id, max_ticks=80, per_tick=None):
    ex = None
    for _ in range(max_ticks):
        ex = c.advance(exec_id)
        if per_tick:
            per_tick(ex)
        if ex["status"] in ("completed", "blocked", "cancelled"):
            return ex
    raise SystemExit(f"{FAIL} 执行 {max_ticks} tick 未结束")


def live(c: DrainClient):
    return c.get_snapshot()["snapshot"]


def main() -> None:
    c = DrainClient(BASE_URL)
    wait_healthy(c)
    print(f"服务就绪: {BASE_URL}\n")

    # ---------- 场景 0：密码协议 ----------
    print("【协议】Ed25519 签名 / 防重放 / 防篡改")
    info = c.http.get("/api/server-key").json()
    server_pub = parse_public_pem(info["public_key_pem"])
    check("服务器公钥算法 Ed25519", info["algorithm"] == "Ed25519")
    check("服务器 kid 长度 32 hex", len(info["kid"]) == 32)

    s0 = snap("proto", [("n1", [dpod("w1", "n1"), dpod("w2", "n1"), dpod("w3", "n1")]), ("n2", [])],
              [dep()], [pdb(min_avail=2)])
    p0 = plan(c, s0, ["n1"])
    body = {k: v for k, v in p0.items() if k != "server_signature"}
    check("计划的服务器签名可验签", verify_object_signature(server_pub, body, p0["server_signature"]["sig"]))
    tampered = dict(body); tampered["drain_nodes"] = ["n2"]
    check("篡改计划后验签失败", not verify_object_signature(server_pub, tampered, p0["server_signature"]["sig"]))

    env = c.build_envelope({"snapshot": s0})
    r1 = c.replay_raw(env, "/api/snapshot")
    r2 = c.replay_raw(env, "/api/snapshot")
    check("一次性 nonce：重放被拒(401)", r1.status_code == 200 and r2.status_code == 401)
    forged = json.loads(json.dumps(env)); forged["payload"]["snapshot"]["snapshot_id"] = "x"
    check("篡改 payload 被拒(401)", c.replay_raw(forged, "/api/snapshot").status_code == 401)
    stale = c.build_envelope({"snapshot": s0}); stale["ts"] = int(time.time()) - 10000
    check("过期时间戳被拒(401)", c.replay_raw(stale, "/api/snapshot").status_code == 401)
    bad_kid = c.build_envelope({}); bad_kid["kid"] = "00" * 16
    check("未知 kid 被拒(401)", c.replay_raw(bad_kid, "/api/sim/tick").status_code == 401)
    print()

    # ---------- 场景 1：已不健康副本 ----------
    print("【场景 1】已不健康副本：PDB 预算为 0，一步都不许动")
    s1 = snap("unhealthy",
              [("n1", [dpod("p1", "n1", ready=True), dpod("p2", "n1", ready=False),
                       dpod("p3", "n1", phase="Pending", ready=False)]),
               ("n2", [])],
              [dep(replicas=3)], [pdb(min_avail=2)])
    p1 = plan(c, s1, ["n1"])
    check("计划状态 blocked", p1["status"] == "blocked")
    reasons = {b["reason"] for b in p1["blocked"]}
    check("阻塞原因含 pdb_allows_zero_disruptions", "pdb_allows_zero_disruptions" in reasons)
    try:
        c.execute(p1["plan_id"])
        check("blocked 计划禁止启动", False)
    except ClientError as e:
        check("blocked 计划禁止启动(400)", "400" in str(e))
    load = c.load_snapshot(s1)
    check("实时 PDB allowed_disruptions=0", load["pdb_status"][0]["allowed_disruptions"] == 0,
          str(load["pdb_status"]))
    print()

    # ---------- 场景 2：两节点同时排空 ----------
    print("【场景 2】两节点同时排空：跨节点共享 PDB 预算，逐 tick 不突破约束")
    s2 = snap("two",
              [("n1", [dpod(f"a{i}", "n1") for i in range(1, 4)]),
               ("n2", [dpod(f"b{i}", "n2") for i in range(1, 4)]),
               ("n3", [])],
              [dep(replicas=6)], [pdb(min_avail=5)])
    p2 = plan(c, s2, ["n1", "n2"])
    per_wave: dict[int, int] = {}
    for st in p2["steps"]:
        if st["kind"] == "evict":
            per_wave[st["wave"]] = per_wave.get(st["wave"], 0) + 1
    check("规划：6 个波次、每波恰好 1 个驱逐", per_wave == {i: 1 for i in range(6)}, str(per_wave))
    check("每个驱逐步骤都有前置条件",
          all(st["preconditions"] for st in p2["steps"] if st["kind"] == "evict"))
    ex2 = c.execute(p2["plan_id"], step_timeout_ticks=30)

    state = {"max_inflight": 0, "min_ready_after_drain": 99}
    def observe(ex):
        inflight = sum(1 for s in ex["steps"] if s["kind"] == "evict" and s["status"] == "in_flight")
        state["max_inflight"] = max(state["max_inflight"], inflight)
        ready = sum(1 for n in live(c)["nodes"] for p in n["pods"]
                    if p["owner_name"] == "web" and p["ready"])
        state["min_ready_after_drain"] = min(state["min_ready_after_drain"], ready)
    ex2 = run(c, ex2["id"], per_tick=observe)
    check("两节点排空 completed", ex2["status"] == "completed", ex2.get("blocked_reason"))
    check("任意时刻在途中断 <= 1", state["max_inflight"] <= 1, str(state["max_inflight"]))
    check("Ready 副本全程不低于 5（minAvailable=5 等效）",
          state["min_ready_after_drain"] >= 5, str(state["min_ready_after_drain"]))
    lv = live(c)
    drained_empty = all(
        not [p for p in n["pods"] if p["owner_name"] == "web"]
        for n in lv["nodes"] if n["name"] in ("n1", "n2"))
    check("n1/n2 已无 web Pod", drained_empty)
    ready_on_n3 = [p for n in lv["nodes"] if n["name"] == "n3" for p in n["pods"]
                   if p["owner_name"] == "web" and p["ready"]]
    check("n3 上 6 个 Ready 替代副本", len(ready_on_n3) == 6, str(len(ready_on_n3)))
    check("n1/n2 最终 cordon",
          all(not n["schedulable"] for n in lv["nodes"] if n["name"] in ("n1", "n2")))
    print()

    # ---------- 场景 3：替代 Pod 迟迟未就绪 ----------
    print("【场景 3】替代 Pod 迟迟未就绪：超时停滞、不推进，故障清除后恢复")
    s3 = snap("slow",
              [("n1", [dpod("w1", "n1"), dpod("w2", "n1"), dpod("w3", "n1")]), ("n2", [])],
              [dep()], [pdb(min_avail=2)])
    p3 = plan(c, s3, ["n1"])
    ex3 = c.execute(p3["plan_id"], step_timeout_ticks=4)
    c.advance(ex3["id"])  # cordon + 第一个驱逐受理
    c.fault("default", "web", "never_ready")
    blocked = False
    for _ in range(12):
        ex3 = c.advance(ex3["id"])
        first_idx = next(s["index"] for s in ex3["steps"] if s["kind"] == "evict")
        later = any(s["status"] in ("in_flight", "done")
                    for s in ex3["steps"] if s["kind"] == "evict" and s["index"] > first_idx)
        check("后续波次绝不启动", not later)
        if ex3["status"] == "blocked":
            blocked = True
            break
    check("执行停滞 blocked", blocked)
    check("停滞原因为替代未就绪超时",
          ex3["blocked_reason"] == "step_timeout:replacement_not_ready",
          ex3["blocked_reason"])
    names = {p["name"] for n in live(c)["nodes"] for p in n["pods"]}
    check("旧 Pod w1 已删除", "w1" not in names)
    not_ready_exists = any(not p["ready"] and p["phase"] == "Running"
                           for n in live(c)["nodes"] for p in n["pods"])
    check("替代 Pod 存在但 NotReady（删除旧 Pod 未被算作补齐）", not_ready_exists)
    # 清除故障 → resume → 完成
    c.fault("default", "web", "clear")
    c.resume(ex3["id"])
    ex3 = run(c, ex3["id"], max_ticks=30)
    check("故障清除后恢复并 completed", ex3["status"] == "completed", ex3.get("blocked_reason"))
    print()

    # ---------- 场景 4：取消 ----------
    print("【场景 4】取消：停止发起新驱逐，可选 uncordon")
    s4 = snap("cancel",
              [("n1", [dpod("w1", "n1"), dpod("w2", "n1"), dpod("w3", "n1")]), ("n2", [])],
              [dep()], [pdb(min_avail=2)])
    p4 = plan(c, s4, ["n1"])
    ex4 = c.execute(p4["plan_id"], uncordon_on_cancel=True)
    c.advance(ex4["id"])  # 第一个驱逐已受理
    n_evict = sum(1 for s in ex4["steps"] if s["kind"] == "evict")
    ex4 = c.cancel(ex4["id"])
    check("状态 cancelled", ex4["status"] == "cancelled")
    cancelled = sum(1 for s in ex4["steps"] if s["status"] == "cancelled")
    check("未开始的驱逐全部 cancelled", cancelled == n_evict - 1, f"{cancelled}/{n_evict - 1}")
    check("uncordon 生效（n1 schedulable=true）",
          next(n for n in live(c)["nodes"] if n["name"] == "n1")["schedulable"] is True)
    before = {s["index"]: s["status"] for s in ex4["steps"]}
    ex4b = c.advance(ex4["id"])
    after = {s["index"]: s["status"] for s in ex4b["steps"]}
    check("取消后 advance 不再产生动作", before == after)
    print()

    # ---------- 汇总 ----------
    print("=" * 60)
    print(f"{PASS} 全部验收通过（{len(results)} 项检查）")
    c.close()


if __name__ == "__main__":
    main()
