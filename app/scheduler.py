"""调度候选分析引擎：先硬约束过滤，再软约束评分。

刻意实现的只是 kube-scheduler 默认行为的明确子集：
- 硬约束（Filter）：nodeSelector、required 节点亲和、NoSchedule/NoExecute 污点、
  资源 request 是否放得下、required Pod 反亲和。
- 软约束（Score，各 0-100 加权平均）：LeastAllocated（按 request）、
  拓扑分布（zone 偏斜）、preferred 节点亲和、preferred Pod 反亲和、
  PreferNoSchedule 污点惩罚。
- 平局按节点名字典序。

不实现：调度队列、抢占、NoExecute 驱逐计时、volume 约束、端口冲突、倾斜的
maxSkew 硬语义（topologySpreadConstraints 的硬版本）等。资源一律按 request
求和，不读取任何实时利用率，也不连接任何真实集群。
"""

from __future__ import annotations

from collections import defaultdict

from .models import (
    AnalyzeRequest,
    AnalyzeResponse,
    ExistingPod,
    NodeResult,
    NodeScoreBreakdown,
    NodeSelectorRequirement,
    NodeSelectorTerm,
    NodeSpec,
    PodAffinityTerm,
    PodSpec,
)

# ---------------------------------------------------------------------------
# 通用匹配
# ---------------------------------------------------------------------------


def match_expression_matches(req: NodeSelectorRequirement, labels: dict[str, str]) -> bool:
    present = req.key in labels
    value = labels.get(req.key)
    if req.operator == "In":
        return present and value in req.values
    if req.operator == "NotIn":
        return (not present) or value not in req.values
    if req.operator == "Exists":
        return present
    if req.operator == "DoesNotExist":
        return not present
    if req.operator in ("Gt", "Lt"):
        if not present or len(req.values) != 1:
            return False
        try:
            label_int = int(value)  # type: ignore[arg-type]
            target = int(req.values[0])
        except ValueError:
            return False
        return label_int > target if req.operator == "Gt" else label_int < target
    return False  # pragma: no cover - Literal 已限定取值


def selector_term_matches(term: NodeSelectorTerm, labels: dict[str, str]) -> bool:
    return all(match_expression_matches(req, labels) for req in term.match_expressions)


def labels_match_selector(selector: dict[str, str], labels: dict[str, str]) -> bool:
    """matchLabels 语义：selector 中每个键值都必须出现在 labels 中。"""
    return all(labels.get(k) == v for k, v in selector.items())


# ---------------------------------------------------------------------------
# 硬约束（Filter）：返回失败原因列表，空列表表示通过
# ---------------------------------------------------------------------------


def check_node_selector(pod: PodSpec, node: NodeSpec) -> list[str]:
    reasons = []
    for key, value in pod.node_selector.items():
        if node.labels.get(key) != value:
            reasons.append(
                f"nodeSelector mismatch: node label {key}={node.labels.get(key)!r} "
                f"!= required {value!r}"
            )
    return reasons


def check_required_node_affinity(pod: PodSpec, node: NodeSpec) -> list[str]:
    terms = pod.node_affinity.required_terms
    if not terms:
        return []
    if any(selector_term_matches(term, node.labels) for term in terms):
        return []
    return ["required node affinity: no nodeSelectorTerm matched node labels"]


def check_taints(pod: PodSpec, node: NodeSpec) -> list[str]:
    """NoSchedule 与 NoExecute 污点未被容忍 => 硬失败；PreferNoSchedule 交给评分。"""
    reasons = []
    for taint in node.taints:
        if taint.effect not in ("NoSchedule", "NoExecute"):
            continue
        if not any(tol.tolerates(taint) for tol in pod.tolerations):
            reasons.append(
                f"untolerated taint: {taint.key}={taint.value}:{taint.effect}"
            )
    return reasons


def check_resources(
    pod: PodSpec, node: NodeSpec, pods_on_node: list[ExistingPod]
) -> list[str]:
    """按 request 求和（pod + 节点上已有 Pod），与 allocatable 比较。"""
    reasons = []
    used_cpu = sum(p.requests.cpu_milli() for p in pods_on_node)
    used_mem = sum(p.requests.memory_bytes() for p in pods_on_node)
    need_cpu = used_cpu + pod.requests.cpu_milli()
    need_mem = used_mem + pod.requests.memory_bytes()
    alloc_cpu = node.allocatable.cpu_milli()
    alloc_mem = node.allocatable.memory_bytes()
    if need_cpu > alloc_cpu:
        reasons.append(
            f"insufficient cpu: requested {need_cpu}m "
            f"(pod {pod.requests.cpu_milli()}m + existing {used_cpu}m) "
            f"> allocatable {alloc_cpu}m"
        )
    if need_mem > alloc_mem:
        reasons.append(
            f"insufficient memory: requested {need_mem}B "
            f"(pod {pod.requests.memory_bytes()}B + existing {used_mem}B) "
            f"> allocatable {alloc_mem}B"
        )
    return reasons


def _anti_affinity_conflicts(
    term: PodAffinityTerm,
    node: NodeSpec,
    nodes_by_name: dict[str, NodeSpec],
    existing_pods: list[ExistingPod],
) -> list[ExistingPod]:
    """同一 topology 域内是否已有 match_labels 命中的 Pod。"""
    topo_value = node.labels.get(term.topology_key)
    if topo_value is None:
        return []
    conflicts = []
    for existing in existing_pods:
        host = nodes_by_name.get(existing.node_name)
        if host is None:
            continue
        if host.labels.get(term.topology_key) != topo_value:
            continue
        if labels_match_selector(term.match_labels, existing.labels):
            conflicts.append(existing)
    return conflicts


def check_required_anti_affinity(
    pod: PodSpec,
    node: NodeSpec,
    nodes_by_name: dict[str, NodeSpec],
    existing_pods: list[ExistingPod],
) -> list[str]:
    reasons = []
    for term in pod.anti_affinity.required_terms:
        if term.topology_key not in node.labels:
            reasons.append(
                f"required pod anti-affinity: node lacks topology key "
                f"{term.topology_key!r}"
            )
            continue
        conflicts = _anti_affinity_conflicts(term, node, nodes_by_name, existing_pods)
        if conflicts:
            names = ", ".join(sorted(p.name for p in conflicts))
            reasons.append(
                f"required pod anti-affinity conflict on {term.topology_key}="
                f"{node.labels[term.topology_key]!r}: existing pod(s) [{names}] "
                f"match {term.match_labels}"
            )
    return reasons


# ---------------------------------------------------------------------------
# 软约束（Score）：各插件返回 0-100
# ---------------------------------------------------------------------------


def score_least_allocated(
    pod: PodSpec, node: NodeSpec, pods_on_node: list[ExistingPod]
) -> float:
    """剩余 request 容量占比（cpu/memory 平均），越空闲越高分。"""
    ratios = []
    used_cpu = sum(p.requests.cpu_milli() for p in pods_on_node) + pod.requests.cpu_milli()
    used_mem = sum(p.requests.memory_bytes() for p in pods_on_node) + pod.requests.memory_bytes()
    alloc_cpu = node.allocatable.cpu_milli()
    alloc_mem = node.allocatable.memory_bytes()
    ratios.append((alloc_cpu - used_cpu) / alloc_cpu if alloc_cpu > 0 else 0.0)
    ratios.append((alloc_mem - used_mem) / alloc_mem if alloc_mem > 0 else 0.0)
    return 100.0 * sum(ratios) / len(ratios)


def score_zone_spread(
    pod: PodSpec,
    node: NodeSpec,
    nodes: list[NodeSpec],
    existing_pods: list[ExistingPod],
) -> float:
    """match_labels 命中的 Pod 在各拓扑域的分布：所在域越空，得分越高。"""
    rule = pod.topology_spread
    if rule is None:
        return 100.0
    if rule.topology_key not in node.labels:
        return 0.0  # 节点缺少拓扑标签，无法参与均衡
    nodes_by_name = {n.name: n for n in nodes}
    counts: dict[str, int] = defaultdict(int)
    for n in nodes:
        value = n.labels.get(rule.topology_key)
        if value is not None:
            counts[value] += 0  # 保证空域也出现在计数里
    for existing in existing_pods:
        host = nodes_by_name.get(existing.node_name)
        if host is None:
            continue
        value = host.labels.get(rule.topology_key)
        if value is None:
            continue
        if labels_match_selector(rule.match_labels, existing.labels):
            counts[value] += 1
    node_count = counts[node.labels[rule.topology_key]]
    lo, hi = min(counts.values()), max(counts.values())
    if hi == lo:
        return 100.0
    return 100.0 * (hi - node_count) / (hi - lo)


def score_preferred_node_affinity(pod: PodSpec, node: NodeSpec) -> float:
    terms = pod.node_affinity.preferred_terms
    if not terms:
        return 100.0
    total = sum(t.weight for t in terms)
    matched = sum(t.weight for t in terms if selector_term_matches(t.term, node.labels))
    return 100.0 * matched / total


def score_preferred_anti_affinity(
    pod: PodSpec,
    node: NodeSpec,
    nodes_by_name: dict[str, NodeSpec],
    existing_pods: list[ExistingPod],
) -> float:
    terms = pod.anti_affinity.preferred_terms
    if not terms:
        return 100.0
    total = sum(t.weight for t in terms)
    violated = 0
    for t in terms:
        if t.term.topology_key not in node.labels:
            violated += t.weight
        elif _anti_affinity_conflicts(t.term, node, nodes_by_name, existing_pods):
            violated += t.weight
    return 100.0 * (total - violated) / total


def score_prefer_no_schedule_taint(pod: PodSpec, node: NodeSpec) -> float:
    for taint in node.taints:
        if taint.effect != "PreferNoSchedule":
            continue
        if not any(tol.tolerates(taint) for tol in pod.tolerations):
            return 0.0
    return 100.0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    pod = request.pod
    nodes_by_name = {n.name: n for n in request.nodes}
    pods_by_node: dict[str, list[ExistingPod]] = defaultdict(list)
    for existing in request.existing_pods:
        pods_by_node[existing.node_name].append(existing)

    results: list[NodeResult] = []
    for node in request.nodes:
        on_node = pods_by_node.get(node.name, [])
        reasons: list[str] = []
        reasons += check_node_selector(pod, node)
        reasons += check_required_node_affinity(pod, node)
        reasons += check_taints(pod, node)
        reasons += check_resources(pod, node, on_node)
        reasons += check_required_anti_affinity(pod, node, nodes_by_name, request.existing_pods)

        if reasons:
            results.append(
                NodeResult(node=node.name, feasible=False, filter_reasons=reasons)
            )
            continue

        breakdown = NodeScoreBreakdown(
            least_allocated=round(score_least_allocated(pod, node, on_node), 2),
            zone_spread=round(
                score_zone_spread(pod, node, request.nodes, request.existing_pods), 2
            ),
            preferred_node_affinity=round(score_preferred_node_affinity(pod, node), 2),
            preferred_anti_affinity=round(
                score_preferred_anti_affinity(
                    pod, node, nodes_by_name, request.existing_pods
                ),
                2,
            ),
            prefer_no_schedule_taint=round(score_prefer_no_schedule_taint(pod, node), 2),
        )
        weights = request.weights
        weighted = [
            (breakdown.least_allocated, weights.least_allocated),
            (breakdown.zone_spread, weights.zone_spread),
            (breakdown.preferred_node_affinity, weights.preferred_node_affinity),
            (breakdown.preferred_anti_affinity, weights.preferred_anti_affinity),
            (breakdown.prefer_no_schedule_taint, weights.prefer_no_schedule_taint),
        ]
        total_weight = sum(w for _, w in weighted)
        final = (
            round(sum(s * w for s, w in weighted) / total_weight, 2)
            if total_weight > 0
            else 0.0
        )
        results.append(
            NodeResult(
                node=node.name,
                feasible=True,
                scores=breakdown,
                final_score=final,
            )
        )

    # 排名：分数降序，平局按节点名字典序（确定性）
    feasible = sorted(
        (r for r in results if r.feasible),
        key=lambda r: (-(r.final_score or 0.0), r.node),
    )
    for rank, result in enumerate(feasible, start=1):
        result.rank = rank

    # 输出顺序：可行节点按名次在前，被淘汰节点按名字在后
    ordered = feasible + sorted(
        (r for r in results if not r.feasible), key=lambda r: r.node
    )
    return AnalyzeResponse(
        pod=pod.name,
        winner=feasible[0].node if feasible else None,
        results=ordered,
    )
