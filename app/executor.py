"""排空执行器。

* 只操作本地 Simulator，不接触真实集群；
* 每个动作执行前对实时快照重新校验前置条件（规划期输出的前置条件不是装饰）；
* 波次门禁：wave N 的任何驱逐必须等 wave N-1 的全部驱逐完成
  （旧 Pod 已消失 + 替代 Pod 已 Ready，删除旧 Pod 绝不被算作补齐）；
* 快照代次（generation）变化时基于最新快照重算剩余步骤；
* 替代 Pod 迟迟不就绪 → step 超时停滞，整个 drain 停止推进，不会继续冒险；
* 取消：停止发起新动作，可选择 uncordon。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .planner import build_plan
from .simulator import PdbAdmissionError, Simulator

STEP_STATUSES = ("pending", "in_flight", "done", "blocked", "cancelled")
EXECUTION_STATUSES = ("running", "blocked", "completed", "cancelled", "cancelling")


@dataclass
class Execution:
    id: str
    plan: dict[str, Any]
    drain_nodes: list[str]
    force: bool
    uncordon_on_cancel: bool
    step_timeout_ticks: int
    status: str = "running"
    blocked_reason: str | None = None
    created_at: float = field(default_factory=time.time)
    baseline_uids: set[str] = field(default_factory=set)
    step_runtime: dict[int, dict[str, Any]] = field(default_factory=dict)
    # (namespace, deployment) -> 已被本执行器驱逐的 pod uid 集合
    evicted_by_dep: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    # 已被某完成步骤“认领”的替代 Pod uid（删除旧 Pod 绝不被算作补齐，
    # 且同一个新 Pod 不能被多个步骤重复认领）
    consumed_replacements: set[str] = field(default_factory=set)
    last_replan_gen: int = 0
    replan_count: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)

    def log_event(self, event_kind: str, **fields: Any) -> None:
        self.log.append({"seq": len(self.log) + 1, "tick": None, "kind": event_kind, **fields})


class Executor:
    def __init__(self, sim: Simulator):
        self.sim = sim
        self._lock = threading.RLock()
        self.executions: dict[str, Execution] = {}

    def create_execution(self, plan: dict[str, Any], options: dict[str, Any]) -> Execution:
        with self._lock:
            if plan["status"] == "blocked" and not options.get("force"):
                raise ValueError("计划状态为 blocked，不能启动（请先解决 blocked 项或显式 force）")
            if plan["snapshot_digest"] != self.sim.digest():
                raise ValueError("计划快照摘要与当前模拟器快照不一致，需基于当前快照重新规划")
            exec_id = "exec-" + uuid.uuid4().hex[:12]
            drain_nodes = list(plan["drain_nodes"])
            ex = Execution(
                id=exec_id,
                plan=plan,
                drain_nodes=drain_nodes,
                force=bool(options.get("force", False)),
                uncordon_on_cancel=bool(options.get("uncordon_on_cancel", False)),
                step_timeout_ticks=int(options.get("step_timeout_ticks", 20)),
                baseline_uids={sp.data["uid"] for sp in self.sim.pods.values()},
                last_replan_gen=self.sim.generation,
            )
            for step in plan["steps"]:
                ex.step_runtime[step["index"]] = {"status": "pending", "started_tick": None, "detail": None, "waiting_since": None}
            self.executions[exec_id] = ex
            ex.log_event("execution_created", execution=exec_id, steps=len(plan["steps"]))
            return ex

    def get(self, exec_id: str) -> Execution:
        if exec_id not in self.executions:
            raise KeyError(f"execution {exec_id} 不存在")
        return self.executions[exec_id]

    # ---------- 取消 / 恢复 ----------

    def cancel(self, exec_id: str) -> Execution:
        with self._lock:
            ex = self.get(exec_id)
            if ex.status in ("completed", "cancelled"):
                return ex
            for step in ex.plan["steps"]:
                rt = ex.step_runtime[step["index"]]
                if rt["status"] == "pending":
                    rt["status"] = "cancelled"
            ex.status = "cancelled"
            ex.blocked_reason = None
            ex.log_event("cancelled")
            if ex.uncordon_on_cancel:
                for node in ex.drain_nodes:
                    if node in self.sim.nodes and not self.sim.nodes[node]["schedulable"]:
                        self.sim.uncordon(node)
            return ex

    def resume(self, exec_id: str) -> Execution:
        """外部条件修复（如清除就绪故障）后解除停滞并重算。"""
        with self._lock:
            ex = self.get(exec_id)
            if ex.status == "blocked":
                for step in ex.plan["steps"]:
                    if ex.step_runtime[step["index"]]["status"] == "blocked":
                        ex.step_runtime[step["index"]] = {
                            "status": "pending",
                            "started_tick": None,
                            "detail": None,
                            "waiting_since": None,
                        }
                ex.status = "running"
                ex.blocked_reason = None
                ex.last_replan_gen = self.sim.generation
                ex.log_event("resumed")
            return ex

    # ---------- 主推进 ----------

    def advance(self, exec_id: str) -> Execution:
        """推进一个 tick 并尽可能多地推进步骤。每动作前都做实时前置校验。"""
        with self._lock:
            ex = self.get(exec_id)
            if ex.status in ("completed", "cancelled"):
                return ex

            # 只有外部动作（故障注入、手工对象漂移等）会让代次与上次结算时不同；
            # 执行器自身上一轮的 evict/cordon bump 在上一轮末尾已被吸收。
            if self.sim.generation != ex.last_replan_gen:
                self._replan(ex)
                if ex.status in ("completed", "blocked", "cancelled"):
                    return ex

            if self._first_unfinished(ex) is None:
                self._complete(ex)
                return ex

            # 先推进模拟时间，让 Pod 消失/重建/就绪（控制器行为不 bump 代次）
            self.sim.tick()

            if ex.status == "cancelling":
                if not self._any_inflight(ex):
                    self._finalize_cancel(ex)
                ex.last_replan_gen = self.sim.generation
                return ex

            self._check_inflight(ex)
            if ex.status != "blocked":
                self._start_ready_steps(ex)

            if self._first_unfinished(ex) is None:
                self._complete(ex)
            else:
                # 状态每轮重算，不锁存：只有存在 blocked 步骤才算停滞；
                # in_flight 等待替代副本属于正常 running（超时由步骤级超时负责）
                ex.status = "blocked" if self._has_blocked_step(ex) else "running"
                if ex.status == "running":
                    ex.blocked_reason = None
            # 吸收本轮自身动作（evict 受理）引起的代次变化
            ex.last_replan_gen = self.sim.generation
            return ex

    # ---------- 步骤状态 ----------

    def _first_unfinished(self, ex: Execution) -> dict[str, Any] | None:
        for step in ex.plan["steps"]:
            if ex.step_runtime[step["index"]]["status"] in ("pending", "in_flight", "blocked"):
                return step
        return None

    def _any_inflight(self, ex: Execution) -> bool:
        return any(
            rt["status"] == "in_flight" for rt in ex.step_runtime.values()
        )

    def _has_blocked_step(self, ex: Execution) -> bool:
        return any(rt["status"] == "blocked" for rt in ex.step_runtime.values())

    def _set_blocked(self, ex: Execution, step: dict[str, Any], reason: str, detail: str) -> None:
        ex.step_runtime[step["index"]].update(status="blocked", detail=detail)
        ex.status = "blocked"
        ex.blocked_reason = reason
        ex.log_event("step_blocked", step=step["index"], reason=reason, detail=detail)

    # ---------- in-flight 检查 ----------

    def _check_inflight(self, ex: Execution) -> None:
        for step in ex.plan["steps"]:
            rt = ex.step_runtime[step["index"]]
            if rt["status"] != "in_flight":
                continue
            if step["kind"] == "evict":
                self._check_evict_completion(ex, step, rt)
            elif step["kind"] in ("cordon", "delete_terminal"):
                # 这两类动作同步完成，正常不会停在 in_flight
                rt["status"] = "done"

    def _check_evict_completion(self, ex: Execution, step: dict[str, Any], rt: dict[str, Any]) -> None:
        ns, name = step["namespace"], step["pod_name"]
        old_pod = self.sim.get_pod(ns, name)
        old_gone = old_pod is None
        if not old_gone:
            detail = "等待原 Pod 完成终止"
            rt["detail"] = detail
            if self.sim.tick_n - (rt["started_tick"] or 0) > ex.step_timeout_ticks:
                self._set_blocked(ex, step, "step_timeout:pod_terminating", detail)
            return

        if step.get("replacement_required"):
            dep_name = step["deployment"]
            ready_new = self._ready_replacement(ex, ns, dep_name, step)
            if not ready_new:
                detail = (
                    f"原 Pod 已删除，但 Deployment {ns}/{dep_name} 尚无 uid 不同的 Ready 替代副本"
                    f"（删除旧 Pod 不算补齐，当前 ready_replicas="
                    f"{self.sim.deployments[(ns, dep_name)]['ready_replicas']}）"
                )
                rt["detail"] = detail
                if self.sim.tick_n - (rt["started_tick"] or 0) > ex.step_timeout_ticks:
                    self._set_blocked(ex, step, "step_timeout:replacement_not_ready", detail)
                return
        if ready_new:
            # 每个驱逐步骤必须对应一个不同的新 Ready Pod（不能拿同一个替代副本补多步）
            ex.consumed_replacements.add(ready_new["uid"])
        rt["status"] = "done"
        rt["detail"] = None
        ex.log_event(
            "step_done",
            step=step["index"],
            kind="evict",
            pod=step["pod"],
            tick=self.sim.tick_n,
        )

    def _ready_replacement(
        self, ex: Execution, ns: str, dep_name: str, step: dict[str, Any]
    ) -> dict[str, Any] | None:
        for sp in self.sim.pods.values():
            d = sp.data
            if (
                d.get("owner_kind") == "Deployment"
                and d.get("owner_name") == dep_name
                and d["namespace"] == ns
                and sp.terminate_tick is None
                and d["phase"] == "Running"
                and d["ready"]
                and d["uid"] not in ex.baseline_uids
                and d["uid"] not in ex.consumed_replacements
            ):
                return d
        return None

    # ---------- 发起新步骤 ----------

    def _start_ready_steps(self, ex: Execution) -> None:
        """同一 tick 内按顺序尝试启动 pending 步骤，直到遇到波次门禁/等待/阻塞。"""
        while True:
            step = None
            for cand in ex.plan["steps"]:
                if ex.step_runtime[cand["index"]]["status"] == "pending":
                    step = cand
                    break
            if step is None:
                return

            # 若前面还有 in_flight 步骤，同波次内其它步骤可以继续，跨波次必须门禁
            if not self._wave_open(ex, step):
                return

            if step["kind"] == "cordon":
                self._do_cordon(ex, step)
            elif step["kind"] == "delete_terminal":
                self._do_delete_terminal(ex, step)
            elif step["kind"] == "evict":
                started = self._do_evict(ex, step)
                if not started:
                    return  # PDB 临时不许可或无调度槽位：停住，等下个 tick
            else:
                self._set_blocked(ex, step, "unknown_step_kind", step["kind"])
                return

    def _wave_open(self, ex: Execution, step: dict[str, Any]) -> bool:
        if step["kind"] != "evict":
            return True
        wave = step["wave"]
        for other in ex.plan["steps"]:
            if other["kind"] != "evict":
                continue
            rt = ex.step_runtime[other["index"]]
            if other["wave"] < wave and rt["status"] not in ("done", "cancelled"):
                return False
        return True

    def _do_cordon(self, ex: Execution, step: dict[str, Any]) -> None:
        node = step["node"]
        n = self.sim.nodes.get(node)
        if n is None:
            self._set_blocked(ex, step, "node_gone", f"node {node} 已不存在")
            return
        self.sim.cordon(node)
        ex.step_runtime[step["index"]]["status"] = "done"
        ex.log_event("step_done", step=step["index"], kind="cordon", node=node)

    def _do_delete_terminal(self, ex: Execution, step: dict[str, Any]) -> None:
        ns, name = step["pod"].split("/", 1)
        sp = self.sim.get_pod(ns, name)
        if sp is None:
            # 已被外部删除，视为完成
            ex.step_runtime[step["index"]]["status"] = "done"
            return
        if sp.data["phase"] not in ("Succeeded", "Failed"):
            self._set_blocked(
                ex, step, "pod_not_terminal", f"pod {step['pod']} 当前 phase={sp.data['phase']}"
            )
            return
        self.sim.delete_terminal(ns, name)
        ex.step_runtime[step["index"]]["status"] = "done"
        ex.log_event("step_done", step=step["index"], kind="delete_terminal", pod=step["pod"])

    def _do_evict(self, ex: Execution, step: dict[str, Any]) -> bool:
        ns, name = step["namespace"], step["pod_name"]
        rt = ex.step_runtime[step["index"]]
        sp = self.sim.get_pod(ns, name)
        if sp is None:
            # 旧 Pod 已被删除：必须有 Ready 替代副本才能算完成，否则软等待
            # （例如故障清除后 resume：替代副本就绪即完成，绝不把删除本身算作补齐）
            if step.get("replacement_required"):
                ready = self._ready_replacement(ex, ns, step["deployment"], step)
                if not ready:
                    return self._soft_wait(
                        ex, step, rt,
                        f"pod {step['pod']} 已消失，但暂无 uid 不同的 Ready 替代副本"
                        f"（删除旧 Pod 不算补齐，ready_replicas="
                        f"{self.sim.deployments[(ns, step['deployment'])]['ready_replicas']}）",
                        "step_timeout:replacement_not_ready",
                    )
                ex.consumed_replacements.add(ready["uid"])
            rt["status"] = "done"
            return True

        # 实时前置条件：节点已 cordon
        if self.sim.nodes[step["node"]]["schedulable"]:
            self._set_blocked(ex, step, "precondition_failed:node_cordoned", "节点未处于 cordon 状态")
            return False

        # 实时前置条件：替代副本有地方可去（Deployment）
        if step.get("replacement_required"):
            if not self._has_free_non_drain_node(ex):
                return self._soft_wait(
                    ex, step, rt,
                    "没有可调度且有空闲槽位的非排空节点承载替代副本",
                    "step_timeout:no_schedulable_node",
                )

        # 实时前置条件：多 PDB 交集 admission（最终防线，与模拟器内检查一致）
        try:
            self.sim.admit_eviction(sp)
        except PdbAdmissionError as e:
            views = {v["pdb"]: {"healthy": v["healthy"], "desired": v["desired_healthy"]} for v in e.denied_views}
            return self._soft_wait(
                ex, step, rt,
                f"PDB 暂不许可: {views}；等待控制器补齐后继续",
                "step_timeout:pdb_budget_exhausted",
            )

        # 执行（真实驱逐）
        self.sim.evict(ns, name)
        rt["status"] = "in_flight"
        rt["started_tick"] = self.sim.tick_n
        rt["waiting_since"] = None
        rt["detail"] = "驱逐已受理，等待原 Pod 终止与替代副本就绪"
        if step.get("replacement_required"):
            ex.evicted_by_dep.setdefault((ns, step["deployment"]), set()).add(step["pod_uid"])
        ex.log_event(
            "step_started",
            step=step["index"],
            kind="evict",
            pod=step["pod"],
            tick=self.sim.tick_n,
        )
        return True

    def _soft_wait(self, ex: Execution, step: dict[str, Any], rt: dict[str, Any],
                   detail: str, timeout_reason: str) -> bool:
        """步骤因前置条件/PDB 预算暂不满足而等待：记录等待起点，超时硬阻塞。"""
        rt["detail"] = detail
        if rt.get("waiting_since") is None:
            rt["waiting_since"] = self.sim.tick_n
            ex.log_event("step_waiting", step=step["index"], detail=detail)
        elif self.sim.tick_n - rt["waiting_since"] > ex.step_timeout_ticks:
            self._set_blocked(ex, step, timeout_reason, detail)
        return False

    def _has_free_non_drain_node(self, ex: Execution) -> bool:
        for node in self.sim.nodes.values():
            if node["name"] in ex.drain_nodes:
                continue
            if node["schedulable"] and len(node["pods"]) < node["capacity"]:
                return True
        return False

    # ---------- 收尾 / 重算 ----------

    def _complete(self, ex: Execution) -> None:
        # 排空完成度校验：排空节点上不允许残留非终态 Pod
        # （结构性 blocked/skipped 项在 force 模式下除外，由调用方显式接受）
        leftovers = []
        for node_name in ex.drain_nodes:
            node = self.sim.nodes.get(node_name)
            if node is None:
                continue
            for uid in node["pods"]:
                sp = self.sim.pods.get(uid)
                if sp is None:
                    continue
                d = sp.data
                if d["phase"] in ("Succeeded", "Failed"):
                    continue
                if sp.terminate_tick is not None:
                    leftovers.append(f"{d['namespace']}/{d['name']}(terminating)")
                    continue
                leftovers.append(f"{d['namespace']}/{d['name']}")
        if leftovers and not ex.force:
            ex.status = "blocked"
            ex.blocked_reason = "drain_incomplete:" + ",".join(sorted(leftovers))
            ex.log_event("drain_incomplete", leftovers=leftovers)
            return
        ex.status = "completed"
        ex.blocked_reason = None
        ex.last_replan_gen = self.sim.generation
        ex.log_event("completed", tick=self.sim.tick_n)

    def _finalize_cancel(self, ex: Execution) -> None:
        ex.status = "cancelled"
        ex.log_event("cancelled", tick=self.sim.tick_n)
        if ex.uncordon_on_cancel:
            for node in ex.drain_nodes:
                if node in self.sim.nodes and not self.sim.nodes[node]["schedulable"]:
                    self.sim.uncordon(node)

    def _replan(self, ex: Execution) -> None:
        """快照代次变化（外部漂移）：基于最新快照宽松重建剩余步骤。

        宽松模式：PDB 预算瞬时为 0 / Pod 正在 terminating 不再硬阻塞，
        由实时 admission 闸门与等待超时把关；结构性不可驱逐项仍进入 blocked。
        已完成/在途步骤保留状态，消失的 pending 步骤标记 cancelled。
        """
        old = ex.plan
        fresh = build_plan(self.sim.export_snapshot(), ex.drain_nodes, force=ex.force, lenient=True)
        new_steps: list[dict[str, Any]] = []
        new_runtime: dict[int, dict[str, Any]] = {}
        old_by_key = {step_key(s): (s, ex.step_runtime[s["index"]]) for s in old["steps"]}

        # 正在终止中的 Pod：即便新计划不再包含该动作，也保留为 in_flight 跟踪其替代副本
        tracked_inflight = {
            step_key(s): (s, rt)
            for s, rt in ((s, ex.step_runtime[s["index"]]) for s in old["steps"])
            if rt["status"] == "in_flight"
        }

        idx = 0
        consumed: set[str] = set()
        for step in fresh["steps"]:
            key = step_key(step)
            consumed.add(key)
            if key in old_by_key:
                _, old_rt = old_by_key[key]
                step["index"] = idx
                new_runtime[idx] = old_rt
            else:
                step["index"] = idx
                new_runtime[idx] = {"status": "pending", "started_tick": None, "detail": None, "waiting_since": None}
            new_steps.append(step)
            idx += 1

        for key, (step, rt) in tracked_inflight.items():
            if key in consumed:
                continue
            # 在途步骤对应的动作消失于新计划（如 Pod 被外部删除）：单独挂账继续等替代副本
            step = dict(step)
            step["index"] = idx
            new_steps.append(step)
            new_runtime[idx] = rt
            idx += 1
            consumed.add(key)

        for s in old["steps"]:
            key = step_key(s)
            if key not in consumed and ex.step_runtime[s["index"]]["status"] not in (
                "done",
                "cancelled",
            ):
                rt = ex.step_runtime[s["index"]]
                rt["status"] = "cancelled"
                rt["detail"] = "快照代次变化后该步骤不再适用"

        fresh["steps"] = new_steps
        ex.plan = fresh
        ex.step_runtime = new_runtime
        ex.last_replan_gen = self.sim.generation
        ex.replan_count += 1
        ex.log_event(
            "replanned",
            generation=self.sim.generation,
            replan_count=ex.replan_count,
            remaining_steps=len(new_steps),
            blocked=fresh["blocked"],
        )
        # 结构性不可驱逐项（DaemonSet/裸 Pod 等）在重算中出现 → 停滞，不硬闯
        if fresh["status"] == "blocked" and not ex.force:
            ex.status = "blocked"
            ex.blocked_reason = "replan_blocked:" + ";".join(b["reason"] for b in fresh["blocked"])

    # ---------- 对外视图 ----------

    def describe(self, ex: Execution) -> dict[str, Any]:
        pdb_now = {v["pdb"]: v for v in self.sim.pdb_status()}
        steps_view = []
        for step in ex.plan["steps"]:
            rt = ex.step_runtime[step["index"]]
            view = {
                "index": step["index"],
                "kind": step["kind"],
                "wave": step["wave"],
                "status": rt["status"],
                "started_tick": rt.get("started_tick"),
                "detail": rt.get("detail"),
            }
            if step["kind"] == "evict":
                view.update(
                    {
                        "pod": step["pod"],
                        "node": step["node"],
                        "covering_pdbs": step["covering_pdbs"],
                        "replacement_required": step["replacement_required"],
                        "preconditions": step["preconditions"],
                        "wave_gate": step["wave_gate"],
                        "completion": step["completion"],
                        "live_pdb": [pdb_now[name] for name in step["covering_pdbs"] if name in pdb_now],
                    }
                )
            else:
                view.update(
                    {
                        "node": step.get("node"),
                        "pod": step.get("pod"),
                        "preconditions": step["preconditions"],
                        "completion": step["completion"],
                    }
                )
            steps_view.append(view)
        return {
            "id": ex.id,
            "status": ex.status,
            "blocked_reason": ex.blocked_reason,
            "drain_nodes": ex.drain_nodes,
            "force": ex.force,
            "uncordon_on_cancel": ex.uncordon_on_cancel,
            "step_timeout_ticks": ex.step_timeout_ticks,
            "tick": self.sim.tick_n,
            "generation": self.sim.generation,
            "replan_count": ex.replan_count,
            "snapshot_digest": ex.plan["snapshot_digest"],
            "steps": steps_view,
            "plan_blocked": ex.plan.get("blocked", []),
            "plan_skipped": ex.plan.get("skipped", []),
            "warnings": ex.plan.get("warnings", []),
            "log": ex.log[-50:],
        }


def step_key(step: dict[str, Any]) -> tuple[Any, ...]:
    if step["kind"] == "cordon":
        return ("cordon", step["node"])
    if step["kind"] == "delete_terminal":
        return ("delete_terminal", step.get("pod"))
    return ("evict", step.get("pod_uid") or step.get("pod"))
