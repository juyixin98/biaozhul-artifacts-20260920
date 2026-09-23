"""节点排空计划器（纯函数，基于本地对象快照）。

PDB 口径对齐 Kubernetes disruption controller：

* selected = namespace 与 selector 匹配、phase 不是 Succeeded/Failed 的 Pod；
* expected(计入副本数) = 其中 deletionTimestamp 为空的 Pod ——
  已进入 terminating 的 Pod 不计入期望副本，但仍作为“在途中断”占名额；
* healthy = expected 中 ready=True 的 Pod；
* desired_healthy：
  - minAvailable 整数 N：N；百分比 p：ceil(expected*p/100)；
  - maxUnavailable 整数 N：expected-N；百分比 p：expected-floor(expected*p/100)；
* 当前允许中断数 = max(0, healthy - desired_healthy - terminating)；
* “再驱逐一个 Pod 是否被许可”按删除发生后的状态重算（K8s apiserver admission 口径）。

多 PDB 交集：一个 Pod 可能同时被多个 PDB 选中，所有覆盖它的 PDB 都必须许可。
"""
from __future__ import annotations

import math
from typing import Any

from .crypto import sha256_hex

TERMINAL_PHASES = ("Succeeded", "Failed")
EVICT_WAVE_BASE = 0
CORDON_WAVE = -2
TERMINAL_WAVE = -1


def labels_match(selector: dict[str, str], labels: dict[str, str]) -> bool:
    return all(labels.get(k) == v for k, v in selector.items())


def _parse_int_or_percent(value: int | str) -> tuple[bool, int]:
    """返回 (is_percent, int_value)。"""
    if isinstance(value, int):
        return False, value
    return True, int(value[:-1])


def desired_healthy(strategy_value: int | str | None, strategy: str, expected: int) -> int:
    """根据 minAvailable / maxUnavailable 计算需要保持就绪的副本数。"""
    is_pct, value = _parse_int_or_percent(strategy_value)
    if strategy == "min_available":
        if is_pct:
            d = math.ceil(expected * value / 100)
        else:
            d = value
    else:  # max_unavailable
        if is_pct:
            unavailable = (expected * value) // 100  # K8s: 向零截断
        else:
            unavailable = value
        d = expected - unavailable
    return max(0, min(expected, d))


def _counts(pods: list[dict[str, Any]], pdb: dict[str, Any]) -> tuple[int, int, int, int]:
    ns = pdb["namespace"]
    selected = [
        p
        for p in pods
        if p["namespace"] == ns
        and p["phase"] not in TERMINAL_PHASES
        and labels_match(pdb["selector"], p["labels"])
    ]
    non_terminating = [p for p in selected if not p.get("deletion_timestamp")]
    expected = len(non_terminating)
    healthy = sum(1 for p in non_terminating if p["ready"])
    terminating = len(selected) - expected
    if pdb.get("min_available") is not None:
        desired = desired_healthy(pdb["min_available"], "min_available", expected)
    else:
        desired = desired_healthy(pdb["max_unavailable"], "max_unavailable", expected)
    return expected, healthy, terminating, desired


def pdb_view(pods: list[dict[str, Any]], pdb: dict[str, Any]) -> dict[str, Any]:
    """单个 PDB 的实时状态（对应 K8s PDB status，allowed 与 admission 同源）。"""
    ns = pdb["namespace"]
    expected, healthy, terminating, desired = _counts(pods, pdb)
    strategy = (
        {"min_available": pdb["min_available"]}
        if pdb.get("min_available") is not None
        else {"max_unavailable": pdb["max_unavailable"]}
    )
    return {
        "pdb": f"{ns}/{pdb['name']}",
        "expected": expected,
        "healthy": healthy,
        "terminating": terminating,
        "desired_healthy": desired,
        "allowed_disruptions": disruptions_allowed(pods, pdb),
        "strategy": strategy,
    }


def disruptions_allowed(pods: list[dict[str, Any]], pdb: dict[str, Any]) -> int:
    """K8s disruption controller / eviction admission 的 disruptionsAllowed。

    对齐 kubernetes/pkg/controller/disruption 的 getDisruptionsAllowed：
      * expected/desiredHealthy 只数非 terminating 的被选中 Pod；
      * minAvailable：desired 是常量（配置整数，或 ceil(expected*p%)）；
      * maxUnavailable：desired = expected - floor(expected*p)（整数 N 即 expected-N）；
      * disruptionsAllowed = healthy - desired - terminating(在途中断数)，
        当 healthy < desired（副本本就不健康）时直接为 0。
    返回值可能为负（在途中断超发的窗口）；admission 要求其 >= 1 才许可。
    """
    _expected, healthy, terminating, desired = _counts(pods, pdb)
    if healthy < desired:
        return 0
    return healthy - desired - terminating




def all_pdb_views(pods: list[dict[str, Any]], pdbs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [pdb_view(pods, each) for each in pdbs]


def covering_pdbs(pod: dict[str, Any], pdbs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        pdb
        for pdb in pdbs
        if pod["namespace"] == pdb["namespace"] and labels_match(pdb["selector"], pod["labels"])
    ]


def denying_pdbs(
    pods: list[dict[str, Any]], pdbs: list[dict[str, Any]], target: dict[str, Any]
) -> list[dict[str, Any]]:
    """返回会拒绝 target 驱逐的 PDB 视图列表（多 PDB 交集：非空即不可驱逐）。

    口径与 K8s eviction admission 完全一致：当前 disruptionsAllowed（见
    disruptions_allowed）必须 > 0 才许可。
    """
    denied: list[dict[str, Any]] = []
    for pdb in covering_pdbs(target, pdbs):
        allowed = disruptions_allowed(pods, pdb)
        if allowed < 1:
            view = pdb_view(pods, pdb)
            view["admission_disruptions_allowed"] = allowed
            denied.append(view)
    return denied


# ---------- 快照规整 ----------

def normalize_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """把 SnapshotSpec.dict() 规整为规划器/模拟器共同使用的内部字典形态。"""
    nodes = []
    for n in snap["nodes"]:
        nodes.append(
            {
                "name": n["name"],
                "schedulable": n.get("schedulable", True),
                "capacity": n.get("capacity", 64),
                "pods": [_pod_dict(p) for p in n.get("pods", [])],
            }
        )
    deployments = []
    for d in snap.get("deployments", []):
        deployments.append(
            {
                "name": d["name"],
                "namespace": d.get("namespace", "default"),
                "replicas": d["replicas"],
                "ready_replicas": d.get("ready_replicas", 0),
                "selector": d["selector"],
                "template_labels": d.get("template_labels") or d["selector"],
                "delay_ready_ticks": d.get("delay_ready_ticks", 1),
            }
        )
    pdbs = [dict(p) for p in snap.get("pdbs", [])]
    return {
        "snapshot_id": snap["snapshot_id"],
        "nodes": nodes,
        "deployments": deployments,
        "pdbs": pdbs,
    }


def _pod_dict(p: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": p["name"],
        "uid": p["uid"],
        "namespace": p.get("namespace", "default"),
        "node": p["node"],
        "phase": p.get("phase", "Running"),
        "ready": bool(p.get("ready", False)),
        "labels": dict(p.get("labels") or {}),
        "owner_kind": p.get("owner_kind"),
        "owner_name": p.get("owner_name"),
        "controller": p.get("controller", True),
        "has_empty_dir": p.get("has_empty_dir", False),
        "deletion_timestamp": p.get("deletion_timestamp"),
    }


def snapshot_digest(snap: dict[str, Any]) -> str:
    """快照内容摘要：对规范化快照（不含摘要字段本身）取 SHA-256。"""
    body = normalize_snapshot(snap) if "nodes" in snap else snap
    return sha256_hex(body)


# ---------- 分类与分批 ----------

def _index_deployments(snap: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(d["namespace"], d["name"]): d for d in snap["deployments"]}


def _all_pods(snap: dict[str, Any]) -> list[dict[str, Any]]:
    return [pod for node in snap["nodes"] for pod in node["pods"]]


def classify_pod(
    pod: dict[str, Any],
    deployments_by_key: dict[tuple[str, str], dict[str, Any]],
) -> tuple[str, str | None]:
    """返回 (分类, 原因)。分类：terminal/evictable/blocked。"""
    if pod["phase"] in TERMINAL_PHASES:
        return "terminal", None
    if pod.get("deletion_timestamp"):
        # 快照里已经在删除的 Pod：等它消失，不产生驱逐动作
        return "blocked", "pod_terminating"
    if pod.get("owner_kind") == "Node":
        return "blocked", "mirror_pod"
    if pod.get("owner_kind") == "DaemonSet":
        return "blocked", "daemonset_pod"
    if pod.get("owner_kind") is not None and not pod.get("controller", True):
        return "blocked", "owner_not_controller"
    if pod.get("owner_kind") is None:
        return "blocked", "bare_pod"
    if pod.get("has_empty_dir"):
        return "blocked", "local_storage_empty_dir"
    if pod.get("owner_kind") == "Deployment":
        key = (pod["namespace"], pod.get("owner_name"))
        if key not in deployments_by_key:
            return "blocked", "deployment_not_found"
        return "evictable", None
    if pod.get("owner_kind") == "ReplicaSet":
        # 无 Deployment 支撑的 ReplicaSet：模拟器不模拟其重建，删除即缩容
        return "evictable", "no_deployment_controller"
    return "blocked", "unsupported_owner"


def _initial_budget(pods, pdbs) -> dict[str, int]:
    return {
        f"{p['namespace']}/{p['name']}": max(0, disruptions_allowed(pods, p))
        for p in pdbs
    }


def _build_waves(
    evictable: list[dict[str, Any]],
    snap: dict[str, Any],
    lenient: bool = False,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """贪心、确定性地把可驱逐 Pod 分配到波次。

    同一波次内，每个 PDB 的驱逐数不得超过其初始 allowedDisruptions；
    没有任何 PDB 覆盖的 Pod，按其所属控制器每波每控制器至多 1 个。
    lenient 模式下，预算瞬时为 0 的 Pod 不阻塞，而是各自独占一个波次，
    由执行器的实时 admission 闸门等待预算恢复。
    返回 (pod_uid -> wave, 零预算阻塞列表)。
    """
    pods = _all_pods(snap)
    budgets = _initial_budget(pods, snap["pdbs"])
    pdb_of: dict[str, list[str]] = {}
    for pod in evictable:
        pdb_of[pod["uid"]] = [
            f"{p['namespace']}/{p['name']}" for p in covering_pdbs(pod, snap["pdbs"])
        ]

    zero_blocked: list[dict[str, Any]] = []
    waves: dict[str, int] = {}
    # wave -> pdb_name -> 已占用数；wave -> owner_key -> 已占用数
    wave_pdb_use: dict[int, dict[str, int]] = {}
    wave_owner_use: dict[int, dict[str, int]] = {}
    next_free_wave = 0

    def fits(w: int, pod, pdb_names, owner_key) -> bool:
        used = wave_pdb_use.setdefault(w, {})
        for name in pdb_names:
            if used.get(name, 0) >= budgets[name]:
                return False
        if not pdb_names:
            if wave_owner_use.setdefault(w, {}).get(owner_key, 0) >= 1:
                return False
        return True

    ordered = sorted(
        evictable,
        key=lambda p: (
            p["namespace"],
            p.get("owner_name") or "",
            p["name"],
        ),
    )
    for pod in ordered:
        pdb_names = pdb_of[pod["uid"]]
        zero = [n for n in pdb_names if budgets[n] <= 0]
        if zero:
            if lenient:
                # 独占一个新波次，等预算恢复后由实时 admission 放行
                next_free_wave += 1
                waves[pod["uid"]] = next_free_wave
                continue
            zero_blocked.append(
                {
                    "pod": f"{pod['namespace']}/{pod['name']}",
                    "node": pod["node"],
                    "reason": "pdb_allows_zero_disruptions",
                    "message": f"PDB {', '.join(sorted(zero))} 当前允许中断数为 0",
                    "covering_pdbs": sorted(zero),
                }
            )
            continue
        owner_key = f"{pod['namespace']}:{pod.get('owner_kind')}:{pod.get('owner_name') or pod['name']}"
        w = 0
        while not fits(w, pod, pdb_names, owner_key):
            w += 1
        waves[pod["uid"]] = w
        next_free_wave = max(next_free_wave, w)
        for name in pdb_names:
            wave_pdb_use.setdefault(w, {})[name] = wave_pdb_use[w].get(name, 0) + 1
        if not pdb_names:
            wave_owner_use.setdefault(w, {})[owner_key] = 1

    return waves, zero_blocked


def _evict_preconditions(pod, deployments_by_key) -> list[dict[str, str]]:
    has_replacement = pod.get("owner_kind") == "Deployment"
    pre = [
        {"rule": "node_cordoned", "expect": f"node {pod['node']}.schedulable == false"},
        {
            "rule": "pod_present",
            "expect": f"pod {pod['namespace']}/{pod['name']} 仍位于 {pod['node']} 且未在删除中",
        },
        {
            "rule": "pdb_intersection_admit",
            "expect": "所有覆盖该 Pod 的 PDB 在驱逐发生后仍满足 healthy >= desired（多 PDB 交集）",
        },
    ]
    if has_replacement:
        dep = deployments_by_key[(pod["namespace"], pod["owner_name"])]
        pre.append(
            {
                "rule": "replacement_schedulable",
                "expect": f"存在非排空、可调度且有空闲槽位的节点承载 Deployment {pod['namespace']}/{dep['name']} 的替代副本",
            }
        )
    return pre


def _evict_completion(pod) -> str:
    if pod.get("owner_kind") == "Deployment":
        return (
            f"原 Pod {pod['namespace']}/{pod['name']} 已消失，并且 Deployment "
            f"{pod['namespace']}/{pod['owner_name']} 在非排空节点上存在一个 uid 不同、"
            f"phase=Running、ready=true 的替代 Pod（删除旧 Pod 不算补齐）"
        )
    return f"Pod {pod['namespace']}/{pod['name']} 已消失（无 Deployment，控制器不保证重建）"


def build_plan(
    snapshot: dict[str, Any],
    drain_nodes: list[str],
    force: bool = False,
    lenient: bool = False,
) -> dict[str, Any]:
    """基于快照构建排空计划。返回可序列化 plan 字典。

    lenient=True 用于执行中的重算：执行器自身的的实时 admission 闸门负责
    拦截瞬时不许可，因此“PDB 预算瞬时为 0”“Pod 正在 terminating”不再
    构成硬阻塞（它们会被序列化到各自波次、等待预算恢复），
    只有结构性不可驱逐项（DaemonSet/裸 Pod 等）仍视为 blocked。
    """
    snap = normalize_snapshot(snapshot)
    node_names = {n["name"] for n in snap["nodes"]}
    missing = sorted(set(drain_nodes) - node_names)
    if missing:
        raise ValueError(f"drain_nodes 不存在于快照: {missing}")
    drain_set = sorted(drain_nodes)
    deployments_by_key = _index_deployments(snap)

    blocked: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    terminals: list[dict[str, Any]] = []
    evictable: list[dict[str, Any]] = []

    transient_terminating: list[dict[str, Any]] = []
    for pod in sorted(_all_pods(snap), key=lambda p: (p["node"], p["namespace"], p["name"])):
        if pod["node"] not in drain_set:
            continue
        kind, reason = classify_pod(pod, deployments_by_key)
        item = {
            "pod": f"{pod['namespace']}/{pod['name']}",
            "node": pod["node"],
            "reason": reason,
        }
        if kind == "terminal":
            terminals.append(pod)
        elif kind == "blocked":
            if lenient and reason == "pod_terminating":
                # 执行中重算：已在删除中的 Pod 等其自然消失，不阻塞也不产生新动作；
                # 在途步骤由执行器单独挂账跟踪替代副本。
                transient_terminating.append(item)
                continue
            item["message"] = _BLOCK_REASONS.get(reason, reason)
            (skipped if force else blocked).append(item)
        else:
            if reason == "no_deployment_controller":
                warnings.append(
                    {
                        "pod": f"{pod['namespace']}/{pod['name']}",
                        "code": "no_deployment_controller",
                        "message": "该 Pod 由裸 ReplicaSet 拥有，模拟器不模拟重建，删除即缩容",
                    }
                )
            evictable.append(pod)

    wave_of, zero_blocked = _build_waves(evictable, snap, lenient=lenient)
    if not force and not lenient:
        blocked.extend(zero_blocked)
    elif force:
        for z in zero_blocked:
            skipped.append(
                {"pod": z["pod"], "node": z["node"], "reason": z["reason"], "message": z["message"]}
            )

    steps: list[dict[str, Any]] = []
    index = 0
    for node in drain_set:
        steps.append(
            {
                "index": index,
                "kind": "cordon",
                "wave": CORDON_WAVE,
                "node": node,
                "preconditions": [
                    {"rule": "node_exists", "expect": f"node {node} 存在于快照中"},
                ],
                "completion": f"node {node} schedulable=false",
            }
        )
        index += 1
    for pod in sorted(terminals, key=lambda p: (p["namespace"], p["name"])):
        steps.append(
            {
                "index": index,
                "kind": "delete_terminal",
                "wave": TERMINAL_WAVE,
                "pod": f"{pod['namespace']}/{pod['name']}",
                "pod_uid": pod["uid"],
                "node": pod["node"],
                "preconditions": [
                    {
                        "rule": "pod_terminal",
                        "expect": f"pod {pod['namespace']}/{pod['name']} phase 为 Succeeded 或 Failed",
                    },
                ],
                "completion": f"Pod {pod['namespace']}/{pod['name']} 已删除",
            }
        )
        index += 1

    planned_uids = set(wave_of)
    ordered_evict = sorted(
        [p for p in evictable if p["uid"] in planned_uids],
        key=lambda p: (wave_of[p["uid"]], p["namespace"], p.get("owner_name") or "", p["name"]),
    )
    max_wave = -1
    for pod in ordered_evict:
        w = wave_of[pod["uid"]]
        max_wave = max(max_wave, w)
        cover = [
            f"{p['namespace']}/{p['name']}" for p in covering_pdbs(pod, snap["pdbs"])
        ]
        steps.append(
            {
                "index": index,
                "kind": "evict",
                "wave": w,
                "pod": f"{pod['namespace']}/{pod['name']}",
                "pod_uid": pod["uid"],
                "namespace": pod["namespace"],
                "pod_name": pod["name"],
                "node": pod["node"],
                "owner_kind": pod.get("owner_kind"),
                "deployment": pod.get("owner_name") if pod.get("owner_kind") == "Deployment" else None,
                "replacement_required": pod.get("owner_kind") == "Deployment",
                "covering_pdbs": cover,
                "preconditions": _evict_preconditions(pod, deployments_by_key),
                "wave_gate": (
                    "无（首个驱逐波次）"
                    if w == 0
                    else f"波次 {w - 1} 的全部驱逐步骤已完成（旧 Pod 消失且替代 Pod Ready）"
                ),
                "completion": _evict_completion(pod),
            }
        )
        index += 1

    status = "blocked" if blocked else "ready"
    return {
        "status": status,
        "snapshot_id": snap["snapshot_id"],
        "drain_nodes": drain_set,
        "force": force,
        "snapshot_digest": sha256_hex(snap),
        "waves": max_wave + 1 if ordered_evict else 0,
        "steps": steps,
        "blocked": blocked,
        "skipped": skipped,
        "warnings": warnings,
    }


_BLOCK_REASONS = {
    "mirror_pod": "mirror Pod（由 kubelet 直接管理）不可驱逐",
    "daemonset_pod": "DaemonSet Pod 不可驱逐（如需忽略请显式 force）",
    "bare_pod": "裸 Pod（无控制器），驱逐后无人重建",
    "owner_not_controller": "Pod 的 ownerReference controller=false，drain 不处理",
    "local_storage_empty_dir": "Pod 使用 emptyDir 本地存储，驱逐会丢数据（如需忽略请显式 force）",
    "deployment_not_found": "Pod 声称属于某 Deployment，但快照中找不到该 Deployment",
    "pod_terminating": "Pod 在快照中已经处于 terminating，等待其消失即可",
}
