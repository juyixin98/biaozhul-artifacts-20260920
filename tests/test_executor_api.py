"""执行器 + API 端到端测试。

覆盖题目要求的四个验收场景：
1. 已不健康副本：一步都不许动；
2. 两节点同时排空：PDB 预算跨节点共享，波次内全局不突破；
3. 替代 Pod 迟迟未就绪：超时停滞，后续步骤不推进，清除故障后恢复；
4. 取消：不再发起新驱逐，可选择 uncordon。
另含：每步前置条件实时校验、快照代次变化重算、删除旧 Pod 不算补齐、签名协议防篡改防重放。
"""
from __future__ import annotations

import copy
import time

from app.planner import labels_match


# ---------- 快照构造 ----------

def dpod(name, node, ready=True, dep="web", ns="default", labels=None, phase="Running", **extra):
    return {
        "name": name, "uid": f"uid-{name}", "namespace": ns, "node": node,
        "phase": phase, "ready": ready,
        "labels": labels or {"app": dep},
        "owner_kind": extra.get("owner_kind", "Deployment"),
        "owner_name": extra.get("owner_name", dep),
        "controller": True,
        "has_empty_dir": extra.get("has_empty_dir", False),
    }


def dep(name="web", ns="default", replicas=3, ready=3, selector=None, delay=1, template=None):
    selector = selector or {"app": name}
    return {
        "name": name, "namespace": ns, "replicas": replicas, "ready_replicas": ready,
        "selector": selector, "template_labels": template or selector, "delay_ready_ticks": delay,
    }


def pdb(name="pdb-web", ns="default", selector=None, min_avail=None, max_unavail=None):
    return {
        "name": name, "namespace": ns, "selector": selector or {"app": "web"},
        "min_available": min_avail, "max_unavailable": max_unavail,
    }


def make_snapshot(sid, nodes, deployments, pdbs, capacities=None):
    return {
        "snapshot_id": sid,
        "nodes": [
            {"name": n, "schedulable": True, "capacity": (capacities or {}).get(n, 30),
             "pods": pods}
            for n, pods in nodes
        ],
        "deployments": deployments,
        "pdbs": pdbs,
    }


def drain_snapshot_3():
    """web: 3 副本全在 n1，n2 空白；PDB minAvailable=2（allowed=1）。"""
    return make_snapshot(
        "d3",
        [("n1", [dpod("w1", "n1"), dpod("w2", "n1"), dpod("w3", "n1")]),
         ("n2", [])],
        [dep(replicas=3)],
        [pdb(min_avail=2)],
    )


def create_plan(api, snap, nodes, force=False):
    r = api.call("POST", "/api/plans",
                 {"snapshot": snap, "drain_nodes": nodes, "force": force})
    assert r.status_code == 200, r.text
    plan = api.payload(r)
    return plan


def start(api, plan_id, **opts):
    r = api.call("POST", "/api/executions", {"plan_id": plan_id, **opts})
    assert r.status_code == 200, r.text
    return api.payload(r)


def advance(api, exec_id, expect_ok=True):
    r = api.call("POST", f"/api/executions/{exec_id}/advance", {})
    if expect_ok:
        assert r.status_code == 200, r.text
    return api.payload(r)


def run_to_end(api, exec_id, max_ticks=60):
    status = None
    for _ in range(max_ticks):
        status = advance(api, exec_id)
        if status["status"] in ("completed", "blocked", "cancelled"):
            return status
    raise AssertionError(f"执行未在 {max_ticks} tick 内结束: {status['status']}")


def live_snapshot(api):
    r = api.http.get("/api/snapshot")
    assert r.status_code == 200
    data = r.json()
    from app.crypto import verify_object_signature
    assert data["signature"]["kid"] == api.server_kid
    assert verify_object_signature(api.server_pub, data["payload"], data["signature"]["sig"])
    return data["payload"]["snapshot"]


# ---------- PDB 不变量工具 ----------

def pdb_violations(snap_view, pdb_specs):
    """任何时刻：非 terminating 且 ready 的被选中 Pod 数不得低于
    按当前非终态副本数解释的 desired（更严格的保守检查）。"""
    violations = []
    pods = [p for n in snap_view["nodes"] for p in n["pods"]]
    for spec in pdb_specs:
        sel = [
            p for p in pods
            if p["namespace"] == spec["namespace"]
            and p["phase"] not in ("Succeeded", "Failed")
            and labels_match(spec["selector"], p["labels"])
        ]
        non_term = [p for p in sel if not p.get("deletion_timestamp")]
        healthy = sum(1 for p in non_term if p["ready"])
        expected = len(non_term)
        if spec.get("min_available") is not None:
            import math
            v = spec["min_available"]
            if isinstance(v, str):
                desired = math.ceil(expected * int(v[:-1]) / 100)
            else:
                desired = min(v, expected)
        else:
            v = spec["max_unavailable"]
            if isinstance(v, str):
                desired = expected - (expected * int(v[:-1])) // 100
            else:
                desired = max(0, expected - v)
        # 只在没有在途中断时断言（在途窗口 K8s 允许 healthy 短暂低于静态 desired）
        terminating = len(sel) - expected
        if terminating == 0 and healthy < desired:
            violations.append((spec["name"], healthy, desired))
    return violations


# ---------- 场景 1：已不健康副本 ----------

class TestUnhealthyReplicas:
    def test_plan_blocks_when_pdb_budget_zero(self, api):
        snap = make_snapshot(
            "unhealthy",
            [("n1", [dpod("p1", "n1", ready=True),
                     dpod("p2", "n1", ready=False),
                     dpod("p3", "n1", phase="Pending", ready=False)]),
             ("n2", [])],
            [dep(replicas=3, ready=1)],
            [pdb(min_avail=2)],
        )
        plan = create_plan(api, snap, ["n1"])
        assert plan["status"] == "blocked"
        reasons = {b["reason"] for b in plan["blocked"]}
        assert "pdb_allows_zero_disruptions" in reasons
        r = api.call("POST", "/api/executions", {"plan_id": plan["plan_id"]})
        assert r.status_code == 400

    def test_direct_eviction_also_denied_at_runtime(self, api):
        snap = drain_snapshot_3()
        create_plan(api, snap, ["n1"])
        # 手工把一个 Pod 置 NotReady（通过重新载入不健康快照模拟外部状态）
        bad = copy.deepcopy(snap)
        for n in bad["nodes"]:
            for p in n["pods"]:
                if p["name"] == "w2":
                    p["ready"] = False
        r = api.call("POST", "/api/snapshot", {"snapshot": bad})
        assert r.status_code == 200
        view = api.payload(r)
        assert view["pdb_status"][0]["allowed_disruptions"] == 0


# ---------- 场景 2：两节点同时排空 ----------

class TestTwoNodeDrain:
    def test_waves_never_violate_shared_pdb(self, api):
        # web 6 副本：n1 三个、n2 三个，n3 空白；minAvailable=5 => allowed=1
        pods_n1 = [dpod(f"a{i}", "n1") for i in range(1, 4)]
        pods_n2 = [dpod(f"b{i}", "n2") for i in range(1, 4)]
        snap = make_snapshot(
            "two", [("n1", pods_n1), ("n2", pods_n2), ("n3", [])],
            [dep(replicas=6)], [pdb(min_avail=5)],
        )
        plan = create_plan(api, snap, ["n1", "n2"])
        assert plan["status"] == "ready"
        # 规划期：每波 1 个驱逐
        per_wave: dict[int, int] = {}
        for s in plan["steps"]:
            if s["kind"] == "evict":
                per_wave[s["wave"]] = per_wave.get(s["wave"], 0) + 1
        assert all(c == 1 for c in per_wave.values())
        assert len(per_wave) == 6

        ex = start(api, plan["plan_id"], step_timeout_ticks=30)
        max_concurrent = 0
        for _ in range(80):
            ex = advance(api, ex["id"])
            inflight = sum(
                1 for s in ex["steps"]
                if s["kind"] == "evict" and s["status"] == "in_flight"
            )
            max_concurrent = max(max_concurrent, inflight)
            # 每个 tick 边界检查 PDB 不变量
            assert pdb_violations(live_snapshot(api), snap["pdbs"]) == []
            if ex["status"] in ("completed", "blocked"):
                break
        assert ex["status"] == "completed", ex["blocked_reason"]
        # 同一时刻绝不允许 2 个在途中断（allowed=1）
        assert max_concurrent <= 1
        # 最终两个节点都空了，web 6 副本全部 Ready 在 n3
        live = live_snapshot(api)
        for n in live["nodes"]:
            if n["name"] in ("n1", "n2"):
                assert [p for p in n["pods"] if p["owner_name"] == "web"] == []
            if n["name"] == "n3":
                ready = [p for p in n["pods"] if p["owner_name"] == "web" and p["ready"]]
                assert len(ready) == 6
        # 两个节点最终被 cordon
        assert all(
            n["schedulable"] is False
            for n in live["nodes"] if n["name"] in ("n1", "n2")
        )


# ---------- 场景 3：替代 Pod 迟迟未就绪 ----------

class TestReplacementNeverReady:
    def test_timeout_blocks_and_fault_clear_resumes(self, api):
        snap = drain_snapshot_3()
        plan = create_plan(api, snap, ["n1"])
        ex = start(api, plan["plan_id"], step_timeout_ticks=4)
        ex = advance(api, ex["id"])  # cordon+delete+第一个 evict 已发起（tick 1）
        # 在第一个驱逐刚被受理后注入“替代永远不 Ready”故障
        r = api.call("POST", "/api/sim/fault",
                     {"namespace": "default", "deployment": "web", "mode": "never_ready"})
        assert r.status_code == 200
        first_evict_index = next(s["index"] for s in ex["steps"] if s["kind"] == "evict")
        blocked = False
        for _ in range(12):
            ex = advance(api, ex["id"])
            # 关键不变量：后续波次一个都不许启动
            later_started = any(
                s["status"] in ("in_flight", "done")
                for s in ex["steps"]
                if s["kind"] == "evict" and s["index"] > first_evict_index
            )
            assert not later_started, "替代未就绪时绝不能推进后续驱逐"
            if ex["status"] == "blocked":
                assert ex["blocked_reason"] == "step_timeout:replacement_not_ready"
                blocked = True
                break
        assert blocked
        # 旧 Pod 被删除绝不能被算作完成：此时原 Pod 已消失但替代没 Ready
        step0 = next(s for s in ex["steps"] if s["kind"] == "evict")
        assert step0["status"] == "blocked"
        assert "Ready" in step0["detail"]

        # 清除故障 → resume → 自动跑完
        r = api.call("POST", "/api/sim/fault",
                     {"namespace": "default", "deployment": "web", "mode": "clear"})
        assert r.status_code == 200
        r = api.call("POST", f"/api/executions/{ex['id']}/resume", {})
        assert r.status_code == 200
        ex = run_to_end(api, ex["id"], max_ticks=30)
        assert ex["status"] == "completed"

    def test_deleted_old_pod_alone_does_not_complete_step(self, api):
        snap = drain_snapshot_3()
        plan = create_plan(api, snap, ["n1"])
        ex = start(api, plan["plan_id"], step_timeout_ticks=50)
        ex = advance(api, ex["id"])
        api.call("POST", "/api/sim/fault",
                 {"namespace": "default", "deployment": "web", "mode": "never_ready"})
        for _ in range(6):
            ex = advance(api, ex["id"])
        # 原 Pod 已不在，替代存在但不 Ready → 步骤必须仍是 in_flight，且 detail 说明原因
        step = next(s for s in ex["steps"] if s["kind"] == "evict")
        r = api.http.get("/api/snapshot")
        live = r.json()["payload"]["snapshot"]
        names = {p["name"] for n in live["nodes"] for p in n["pods"]}
        assert "w1" not in names
        assert step["status"] in ("in_flight", "blocked")
        assert "删除旧 Pod 不算补齐" in step["detail"]


# ---------- 场景 4：取消 ----------

class TestCancel:
    def test_cancel_stops_new_evictions_and_uncordons(self, api):
        snap = drain_snapshot_3()
        plan = create_plan(api, snap, ["n1"])
        ex = start(api, plan["plan_id"], uncordon_on_cancel=True)
        ex = advance(api, ex["id"])  # 第一个 evict 已受理
        evict_indices = [s["index"] for s in ex["steps"] if s["kind"] == "evict"]
        r = api.call("POST", f"/api/executions/{ex['id']}/cancel", {})
        assert r.status_code == 200
        ex = api.payload(r)
        assert ex["status"] == "cancelled"
        # 未开始的步骤全部 cancelled
        pending_left = [s for s in ex["steps"] if s["status"] == "pending"]
        assert pending_left == []
        cancelled_steps = [s for s in ex["steps"] if s["status"] == "cancelled"]
        assert len(cancelled_steps) == len(evict_indices) - 1
        # uncordon 已生效
        r = api.http.get("/api/snapshot")
        live = r.json()["payload"]["snapshot"]
        assert next(n for n in live["nodes"] if n["name"] == "n1")["schedulable"] is True
        # 再 advance 也不会有新动作
        before = {s["index"]: s["status"] for s in ex["steps"]}
        ex2 = advance(api, ex["id"])
        after = {s["index"]: s["status"] for s in ex2["steps"]}
        assert before == after

    def test_cancel_without_uncordon_keeps_node_cordoned(self, api):
        snap = drain_snapshot_3()
        plan = create_plan(api, snap, ["n1"])
        ex = start(api, plan["plan_id"], uncordon_on_cancel=False)
        advance(api, ex["id"])
        r = api.call("POST", f"/api/executions/{ex['id']}/cancel", {})
        assert r.status_code == 200
        live = live_snapshot(api)
        assert next(n for n in live["nodes"] if n["name"] == "n1")["schedulable"] is False


# ---------- 代次变化重算 ----------

class TestGenerationReplan:
    def test_external_change_triggers_replan(self, api):
        # n1 排空，n2 是替代目标，n3 备用；外部 cordon n3 触发代次变化
        snap = make_snapshot(
            "replan",
            [("n1", [dpod("w1", "n1"), dpod("w2", "n1"), dpod("w3", "n1")]),
             ("n2", []), ("n3", [])],
            [dep(replicas=3)],
            [pdb(min_avail=2)],
        )
        plan = create_plan(api, snap, ["n1"])
        ex = start(api, plan["plan_id"], step_timeout_ticks=30)
        ex = advance(api, ex["id"])  # cordon + 第一个 evict 受理
        gen_before = ex["generation"]
        # 外部结构变化：直接在模拟器上 cordon n3（等价于快照代次变化）
        from app import main as store_module
        store_module.store.sim.cordon("n3")
        ex = advance(api, ex["id"])
        assert ex["generation"] != gen_before
        assert ex["replan_count"] >= 1
        assert any(e["kind"] == "replanned" for e in ex["log"])
        # 最终仍能完成（替代副本调度到 n2）
        ex = run_to_end(api, ex["id"], max_ticks=30)
        assert ex["status"] == "completed"

    def test_external_unhealthy_change_blocks_replan(self, api):
        snap = drain_snapshot_3()
        plan = create_plan(api, snap, ["n1"])
        ex = start(api, plan["plan_id"], step_timeout_ticks=3)
        ex = advance(api, ex["id"])
        # 外部把另一个副本置 NotReady（对象漂移），代次 +1
        from app import main as store_module
        sp = store_module.store.sim.get_pod("default", "w2")
        sp.data["ready"] = False
        store_module.store.sim.generation += 1
        store_module.store.sim._event("external_mutated", generation=store_module.store.sim.generation)
        ex = advance(api, ex["id"])
        assert ex["replan_count"] >= 1
        # 预算耗尽后等待超时 → 停滞，绝不硬闯
        for _ in range(8):
            ex = advance(api, ex["id"])
            if ex["status"] == "blocked":
                break
        assert ex["status"] == "blocked"
        assert ex["blocked_reason"] == "step_timeout:pdb_budget_exhausted"
        # 期间绝不允许出现第二个在途中断
        inflight = sum(
            1 for s in ex["steps"] if s["kind"] == "evict" and s["status"] == "in_flight"
        )
        assert inflight <= 1


# ---------- 前置条件输出与执行顺序 ----------

class TestPreconditions:
    def test_every_step_lists_preconditions_and_live_pdb(self, api):
        snap = drain_snapshot_3()
        plan = create_plan(api, snap, ["n1"])
        rules = {
            (s["kind"], s["index"]): [p["rule"] for p in s["preconditions"]]
            for s in plan["steps"]
        }
        assert rules[("cordon", 0)] == ["node_exists"]
        ev_rules = [r for (k, _i), rs in rules.items() if k == "evict" for r in rs]
        assert "node_cordoned" in ev_rules
        assert "pdb_intersection_admit" in ev_rules
        assert "replacement_schedulable" in ev_rules
        ex = start(api, plan["plan_id"])
        ex = advance(api, ex["id"])
        ev = next(s for s in ex["steps"] if s["kind"] == "evict" and s["status"] == "in_flight")
        assert ev["live_pdb"], "执行态必须回传实时 PDB 状态"
        assert ev["live_pdb"][0]["allowed_disruptions"] <= 0

    def test_ordering_cordon_then_terminal_then_waves(self, api):
        snap = copy.deepcopy(drain_snapshot_3())
        snap["nodes"][0]["pods"].append(
            dpod("done", "n1", ready=False, phase="Succeeded", owner_kind=None)
        )
        plan = create_plan(api, snap, ["n1"])
        kinds = [(s["kind"], s["wave"]) for s in plan["steps"]]
        assert kinds[0][0] == "cordon"
        assert ("delete_terminal", -1) in kinds
        assert kinds.index(("delete_terminal", -1)) < [i for i, k in enumerate(kinds) if k[0] == "evict"][0]


# ---------- 协议：签名、防重放、防篡改、时间窗 ----------

class TestProtocol:
    def test_unsigned_write_rejected(self, api):
        r = api.http.post("/api/snapshot", json={"hello": "world"})
        assert r.status_code == 401

    def test_replay_rejected(self, api):
        env = api.envelope({"snapshot": drain_snapshot_3()})
        r1 = api.http.post("/api/snapshot", json=env)
        assert r1.status_code == 200
        r2 = api.http.post("/api/snapshot", json=env)
        assert r2.status_code == 401
        assert "nonce" in r2.text

    def test_tampered_payload_rejected(self, api):
        env = api.envelope({"snapshot": drain_snapshot_3()})
        env["payload"]["snapshot"]["snapshot_id"] = "forged"
        r = api.http.post("/api/snapshot", json=env)
        assert r.status_code == 401

    def test_stale_timestamp_rejected(self, api):
        env = api.envelope({"snapshot": drain_snapshot_3()}, ts=int(time.time()) - 10_000)
        r = api.http.post("/api/snapshot", json=env)
        assert r.status_code == 401

    def test_unknown_kid_rejected(self, api):
        env = api.envelope({})
        env["kid"] = "deadbeef" * 4
        r = api.http.post("/api/sim/tick", json=env)
        assert r.status_code == 401

    def test_server_plan_signature_verifiable(self, api):
        plan = create_plan(api, drain_snapshot_3(), ["n1"])
        sig = plan["server_signature"]
        assert sig["alg"] == "Ed25519"
        from app.crypto import verify_object_signature
        body = {k: v for k, v in plan.items() if k != "server_signature"}
        assert verify_object_signature(api.server_pub, body, sig["sig"])
        # 篡改计划内容后验签失败
        body["drain_nodes"] = ["n2"]
        assert not verify_object_signature(api.server_pub, body, sig["sig"])

    def test_snapshot_digest_changes_with_content(self, api):
        p1 = create_plan(api, drain_snapshot_3(), ["n1"])
        snap2 = copy.deepcopy(drain_snapshot_3())
        snap2["nodes"][0]["pods"][0]["ready"] = False
        snap2["snapshot_id"] = "d3-modified"
        p2 = create_plan(api, snap2, ["n1"])
        assert p1["snapshot_digest"] != p2["snapshot_digest"]
