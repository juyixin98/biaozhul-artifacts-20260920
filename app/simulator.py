"""本地集群模拟器。

不调用任何真实 Kubernetes API，但语义上真实地模拟：

* cordon / uncordon（node schedulable 翻转）；
* evict：经 PDB admission 检查（多 PDB 交集）后把 Pod 置为 terminating，
  grace 期满后删除；
* Deployment 控制器：每个 tick 对账副本数，在可调度、非排空节点上创建替代 Pod；
* Pod 生命周期：Pending（无可调度节点时挂起）→ Running → Ready（延迟可调）；
* 故障注入：让某 Deployment 的替代 Pod 永远 NotReady，或延长就绪延迟；
* generation（快照代次）：外部/结构性动作 +1（载入快照、cordon/uncordon、
  驱逐受理、终态 Pod 删除、外部对象漂移）；控制器对账（替代 Pod 创建）、
  grace 期满删除、就绪流转属于正常控制器行为，不 bump；
  故障注入是执行器预期内的控制操作，单独计 fault_generation，不触发重算。
  执行器据此区分“外部对象漂移（需重算）”与“自己动作的自然后果”。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .planner import (
    TERMINAL_PHASES,
    all_pdb_views,
    denying_pdbs,
    labels_match,
    normalize_snapshot,
    snapshot_digest,
)

GRACE_TICKS = 1


class PdbAdmissionError(Exception):
    """模拟 apiserver 的 eviction admission 拒绝。"""

    def __init__(self, denied_views: list[dict[str, Any]]):
        self.denied_views = denied_views
        names = ", ".join(v["pdb"] for v in denied_views)
        super().__init__(f"eviction 被 PDB 拒绝: {names}")


class SimPod:
    def __init__(self, data: dict[str, Any], created_tick: int = 0):
        self.data = data
        self.created_tick = created_tick
        self.terminate_tick: int | None = None
        self.stuck_not_ready = False

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.data)
        if self.terminate_tick is not None:
            d["deletion_timestamp"] = f"tick-{self.terminate_tick}"
        else:
            d["deletion_timestamp"] = None
        return d


class Simulator:
    def __init__(self) -> None:
        self.snapshot_id: str = ""
        self.nodes: dict[str, dict[str, Any]] = {}
        self.pods: dict[str, SimPod] = {}
        self.deployments: dict[tuple[str, str], dict[str, Any]] = {}
        self.pdbs: list[dict[str, Any]] = []
        self.tick_n = 0
        self.generation = 0
        self.fault_generation = 0
        self.uid_counter = 0
        self.events: list[dict[str, Any]] = []
        # 故障：deployment key -> {"mode": "never"|"delay", "delay": int}
        self.faults: dict[tuple[str, str], dict[str, Any]] = {}

    # ---------- 基础 ----------

    def load_snapshot(self, snap: dict[str, Any]) -> None:
        snap = normalize_snapshot(snap)
        self.snapshot_id = snap["snapshot_id"]
        self.nodes = {n["name"]: n for n in deepcopy(snap["nodes"])}
        self.pods = {}
        self.deployments = {(d["namespace"], d["name"]): deepcopy(d) for d in snap["deployments"]}
        self.pdbs = deepcopy(snap["pdbs"])
        for node in self.nodes.values():
            node["pods"] = []
        for pdata in snap_list_pods(snap):
            sp = SimPod(deepcopy(pdata), created_tick=0)
            if sp.data.get("deletion_timestamp"):
                sp.terminate_tick = 0
            self.pods[sp.data["uid"]] = sp
            self.nodes[sp.data["node"]]["pods"].append(sp.data["uid"])
        self.tick_n = 0
        self.uid_counter = 0
        self.faults = {}
        self.fault_generation = 0
        self.generation += 1
        self._event("snapshot_loaded", snapshot_id=self.snapshot_id, generation=self.generation)
        self._refresh_ready_replicas()

    def digest(self) -> str:
        return snapshot_digest(self.export_snapshot())

    def export_snapshot(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "nodes": [
                {
                    "name": n["name"],
                    "schedulable": n["schedulable"],
                    "capacity": n["capacity"],
                    "pods": [self.pods[u].as_dict() for u in n["pods"] if u in self.pods],
                }
                for n in self.nodes.values()
            ],
            "deployments": list(self.deployments.values()),
            "pdbs": deepcopy(self.pdbs),
        }

    def all_pod_dicts(self) -> list[dict[str, Any]]:
        return [sp.as_dict() for sp in self.pods.values()]

    def get_pod(self, namespace: str, name: str) -> SimPod | None:
        for sp in self.pods.values():
            d = sp.data
            if d["namespace"] == namespace and d["name"] == name:
                return sp
        return None

    def pdb_status(self) -> list[dict[str, Any]]:
        return all_pdb_views(self.all_pod_dicts(), self.pdbs)

    def get_events(self, after_seq: int = 0) -> list[dict[str, Any]]:
        return [e for e in self.events if e["seq"] > after_seq]

    def _event(self, kind: str, **fields: Any) -> dict[str, Any]:
        ev = {"seq": len(self.events) + 1, "tick": self.tick_n, "kind": kind, **fields}
        self.events.append(ev)
        if len(self.events) > 1000:
            del self.events[: len(self.events) - 1000]
        return ev

    # ---------- 节点操作 ----------

    def cordon(self, node: str) -> None:
        n = self._node(node)
        if n["schedulable"]:
            n["schedulable"] = False
            self.generation += 1
            self._event("node_cordoned", node=node, generation=self.generation)

    def uncordon(self, node: str) -> None:
        n = self._node(node)
        if not n["schedulable"]:
            n["schedulable"] = True
            self.generation += 1
            self._event("node_uncordoned", node=node, generation=self.generation)

    def _node(self, name: str) -> dict[str, Any]:
        if name not in self.nodes:
            raise KeyError(f"node {name} 不存在")
        return self.nodes[name]

    # ---------- Pod 操作 ----------

    def delete_terminal(self, namespace: str, name: str) -> None:
        sp = self.get_pod(namespace, name)
        if sp is None:
            raise KeyError(f"pod {namespace}/{name} 不存在")
        if sp.data["phase"] not in TERMINAL_PHASES:
            raise ValueError(f"pod {namespace}/{name} 不是终态 Pod（phase={sp.data['phase']}）")
        self._remove(sp, "terminal_pod_deleted")

    def admit_eviction(self, sp: SimPod) -> None:
        """PDB admission（真实校验，拒绝即抛错）。"""
        if sp.terminate_tick is not None:
            raise ValueError(f"pod {sp.data['namespace']}/{sp.data['name']} 已在删除中")
        if sp.data["phase"] in TERMINAL_PHASES:
            raise ValueError("终态 Pod 应走 delete 而非 evict")
        denied = denying_pdbs(self.all_pod_dicts(), self.pdbs, sp.as_dict())
        if denied:
            self._event(
                "eviction_denied",
                pod=f"{sp.data['namespace']}/{sp.data['name']}",
                pdbs=[
                    {"pdb": v["pdb"], "healthy": v["healthy"], "desired": v["desired_healthy"]}
                    for v in denied
                ],
            )
            raise PdbAdmissionError(denied)

    def evict(self, namespace: str, name: str) -> None:
        sp = self.get_pod(namespace, name)
        if sp is None:
            raise KeyError(f"pod {namespace}/{name} 不存在")
        self.admit_eviction(sp)
        sp.terminate_tick = self.tick_n
        sp.data["ready"] = False
        self.generation += 1
        self._event(
            "eviction_admitted",
            pod=f"{namespace}/{name}",
            uid=sp.data["uid"],
            node=sp.data["node"],
            generation=self.generation,
            pdbs=self._pdb_snapshot_for(sp),
        )

    def _pdb_snapshot_for(self, sp: SimPod) -> list[dict[str, Any]]:
        names = {
            f"{p['namespace']}/{p['name']}"
            for p in self.pdbs
            if sp.data["namespace"] == p["namespace"]
            and labels_match(p["selector"], sp.data["labels"])
        }
        return [v for v in self.pdb_status() if v["pdb"] in names]

    def _remove(self, sp: SimPod, event_kind: str, bump_generation: bool = True) -> None:
        uid = sp.data["uid"]
        node = sp.data["node"]
        self.nodes[node]["pods"] = [u for u in self.nodes[node]["pods"] if u != uid]
        del self.pods[uid]
        if bump_generation:
            self.generation += 1
        self._event(
            event_kind,
            pod=f"{sp.data['namespace']}/{sp.data['name']}",
            uid=uid,
            node=node,
            generation=self.generation,
        )

    # ---------- 故障注入（仅模拟器） ----------

    def inject_fault(self, namespace: str, deployment: str, mode: str, delay: int | None = None) -> None:
        key = (namespace, deployment)
        if key not in self.deployments:
            raise KeyError(f"deployment {namespace}/{deployment} 不存在")
        if mode == "never_ready":
            self.faults[key] = {"mode": "never", "delay": None}
        elif mode == "delay_ready":
            self.faults[key] = {"mode": "delay", "delay": int(delay or 5)}
        else:
            raise ValueError(f"未知故障模式 {mode}")
        for sp in self.pods.values():
            d = sp.data
            if (
                d.get("owner_kind") == "Deployment"
                and (d["namespace"], d.get("owner_name")) == key
                and d.get("created_after_drain")
            ):
                sp.stuck_not_ready = True if mode == "never_ready" else sp.stuck_not_ready
        self.fault_generation += 1
        self._event(
            "fault_injected",
            deployment=f"{namespace}/{deployment}",
            mode=mode,
            delay=delay,
            fault_generation=self.fault_generation,
        )

    def clear_fault(self, namespace: str, deployment: str) -> None:
        key = (namespace, deployment)
        removed = self.faults.pop(key, None)
        if removed is None:
            return
        for sp in self.pods.values():
            d = sp.data
            if (
                d.get("owner_kind") == "Deployment"
                and (d["namespace"], d.get("owner_name")) == key
                and d.get("created_after_drain")
            ):
                sp.stuck_not_ready = False
                sp.created_tick = self.tick_n  # 重新开始计就绪延迟
        self.fault_generation += 1
        self._event("fault_cleared", deployment=f"{namespace}/{deployment}", fault_generation=self.fault_generation)

    # ---------- tick ----------

    def tick(self) -> dict[str, Any]:
        """推进一个模拟时间单位，返回该 tick 的事件。"""
        self.tick_n += 1
        started = len(self.events)

        # 1) grace 期满的 terminating Pod 真正消失（控制器行为，不 bump 代次）
        for sp in list(self.pods.values()):
            if (
                sp.terminate_tick is not None
                and self.tick_n - sp.terminate_tick >= GRACE_TICKS
            ):
                self._remove(sp, "pod_terminated", bump_generation=False)

        # 2) Deployment 控制器对账 + 调度
        self._reconcile()

        # 3) Pod 就绪状态流转（只管理模拟器自己创建的替代 Pod；
        #    快照原有 Pod 的 phase/ready 是观测状态，不被模拟器擅自改写）
        for sp in self.pods.values():
            d = sp.data
            if not d.get("created_after_drain"):
                continue
            if sp.terminate_tick is not None or d["phase"] in TERMINAL_PHASES:
                continue
            if d["phase"] == "Running":
                delay = self._ready_delay(d)
                if not d["ready"] and (self.tick_n - sp.created_tick) >= delay:
                    if not sp.stuck_not_ready:
                        d["ready"] = True
                        self._event("pod_ready", pod=f"{d['namespace']}/{d['name']}", uid=d["uid"])
            elif d["phase"] == "Pending":
                target = self._schedule(d)
                if target is not None:
                    d["phase"] = "Running"
                    d["node"] = target
                    sp.created_tick = self.tick_n
                    self.nodes[target]["pods"].append(d["uid"])
                    self._event(
                        "pod_scheduled",
                        pod=f"{d['namespace']}/{d['name']}",
                        uid=d["uid"],
                        node=target,
                    )
        self._refresh_ready_replicas()
        return {"tick": self.tick_n, "events": self.events[started:]}

    def _ready_delay(self, d: dict[str, Any]) -> int:
        dep = self.deployments.get((d["namespace"], d.get("owner_name")))
        base = dep["delay_ready_ticks"] if dep else 1
        fault = self.faults.get((d["namespace"], d.get("owner_name")))
        if fault and fault["mode"] == "delay":
            return fault["delay"]
        return base

    def _reconcile(self) -> None:
        for (ns, name), dep in self.deployments.items():
            live = [
                sp
                for sp in self.pods.values()
                if sp.data.get("owner_kind") == "Deployment"
                and sp.data.get("owner_name") == name
                and sp.data["namespace"] == ns
                and sp.terminate_tick is None
                and sp.data["phase"] not in TERMINAL_PHASES
            ]
            missing = dep["replicas"] - len(live)
            for _ in range(max(0, missing)):
                self._create_replacement(dep)

    def _create_replacement(self, dep: dict[str, Any]) -> None:
        labels = dict(dep["template_labels"])
        self.uid_counter += 1
        ns, name = dep["namespace"], dep["name"]
        pod_name = f"{name}-sim-{self.tick_n}-{self.uid_counter}"
        uid = f"sim-uid-{self.uid_counter}-{self.tick_n}"
        data: dict[str, Any] = {
            "name": pod_name,
            "uid": uid,
            "namespace": ns,
            "node": "",
            "phase": "Pending",
            "ready": False,
            "labels": labels,
            "owner_kind": "Deployment",
            "owner_name": name,
            "controller": True,
            "has_empty_dir": False,
            "deletion_timestamp": None,
            "created_after_drain": True,
        }
        sp = SimPod(data, created_tick=self.tick_n)
        if self.faults.get((ns, name), {}).get("mode") == "never":
            sp.stuck_not_ready = True
        self.pods[uid] = sp
        # 控制器对账产生的替代 Pod 是正常控制器行为，不 bump 代次
        self._event(
            "replacement_created",
            deployment=f"{ns}/{name}",
            pod=f"{ns}/{pod_name}",
            uid=uid,
            generation=self.generation,
        )

    def _schedule(self, d: dict[str, Any]) -> str | None:
        for node in self.nodes.values():
            if not node["schedulable"]:
                continue
            if len(node["pods"]) >= node["capacity"]:
                continue
            return node["name"]
        return None

    def _refresh_ready_replicas(self) -> None:
        for (ns, name), dep in self.deployments.items():
            dep["ready_replicas"] = sum(
                1
                for sp in self.pods.values()
                if sp.data.get("owner_kind") == "Deployment"
                and sp.data.get("owner_name") == name
                and sp.data["namespace"] == ns
                and sp.terminate_tick is None
                and sp.data["phase"] == "Running"
                and sp.data["ready"]
            )


def snap_list_pods(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return [p for node in snap["nodes"] for p in node["pods"]]
