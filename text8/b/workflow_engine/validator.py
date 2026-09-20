"""模板定义校验：结构、引用完整性、可达性、环路、受限表达式。

发布前执行；draft 不校验。返回结构化问题列表，拒绝发布任何一个 error。
"""

from dataclasses import dataclass

from workflow_engine.constants import NodeType, TimeoutAction
from workflow_engine.expressions import ExpressionError, parse_expression


@dataclass
class ValidationIssue:
    node_id: str | None
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"node_id": self.node_id, "code": self.code, "message": self.message}


def _node_refs(node: dict) -> list[str]:
    """节点的所有出边（目标节点 id）。"""
    t = node["type"]
    if t in (NodeType.START, NodeType.APPROVAL):
        refs = [node["next"]]
        if node.get("on_reject"):
            refs.append(node["on_reject"])
        return refs
    if t == NodeType.CONDITION:
        refs = [b["next"] for b in node["branches"]]
        if node.get("default"):
            refs.append(node["default"])
        return refs
    return []


def validate_definition(definition: dict) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    nodes = definition.get("nodes", [])

    starts = [n for n in nodes if n["type"] == NodeType.START]
    ends = [n for n in nodes if n["type"] == NodeType.END]

    if len(starts) != 1:
        issues.append(ValidationIssue(None, "start_count", f"必须恰好有 1 个 start 节点（当前 {len(starts)} 个）"))
    if not ends:
        issues.append(ValidationIssue(None, "end_count", "至少需要 1 个 end 节点"))

    # 节点 id 唯一
    ids: dict[str, dict] = {}
    for n in nodes:
        nid = n["id"]
        if nid in ids:
            issues.append(ValidationIssue(nid, "duplicate_node", f"节点 id 重复: {nid}"))
        ids[nid] = n

    if issues:
        return issues  # 后续检查依赖唯一 id

    start_id = starts[0]["id"] if starts else None

    # end 节点不允许出边
    for n in nodes:
        if n["type"] == NodeType.END:
            extra = [k for k in ("next", "on_reject", "branches", "default") if n.get(k)]
            if extra:
                issues.append(
                    ValidationIssue(n["id"], "end_has_edges", f"end 节点不能有出边: {', '.join(extra)}")
                )

    # 引用完整性 + 不允许指向 start
    for n in nodes:
        for ref in _node_refs(n):
            if ref not in ids:
                issues.append(ValidationIssue(n["id"], "missing_reference", f"引用了不存在的节点: {ref}"))
            elif ids[ref]["type"] == NodeType.START:
                issues.append(ValidationIssue(n["id"], "edge_to_start", f"边不能指向 start 节点: {ref}"))

    # 审批节点专属校验
    for n in nodes:
        if n["type"] != NodeType.APPROVAL:
            continue
        approvers = n.get("approvers", [])
        if len(approvers) != len(set(approvers)):
            issues.append(ValidationIssue(n["id"], "duplicate_approver", "审批人列表存在重复"))
        action = n.get("timeout_action")
        timeout = n.get("timeout_seconds")
        if timeout is not None and action is None:
            issues.append(ValidationIssue(n["id"], "timeout_without_action", "配置了 timeout_seconds 必须同时配置 timeout_action"))
        if action is not None and timeout is None:
            issues.append(ValidationIssue(n["id"], "action_without_timeout", "配置了 timeout_action 必须同时配置 timeout_seconds"))
        if action and action["type"] == TimeoutAction.ESCALATE:
            targets = action.get("to", [])
            if not targets:
                issues.append(ValidationIssue(n["id"], "empty_escalation", "升级目标不能为空"))
            dup = set(targets) & set(approvers)
            if dup:
                issues.append(
                    ValidationIssue(n["id"], "escalation_overlap", f"升级目标不能与原审批人重复: {sorted(dup)}")
                )
            if len(targets) != len(set(targets)):
                issues.append(ValidationIssue(n["id"], "duplicate_escalation", "升级目标列表存在重复"))

    # 条件表达式语法（发布期静态检查；运行期再求值）
    for n in nodes:
        if n["type"] != NodeType.CONDITION:
            continue
        for b in n["branches"]:
            try:
                parse_expression(b["when"])
            except ExpressionError as exc:
                issues.append(ValidationIssue(n["id"], "bad_expression", f"分支 {b['next']} 表达式非法: {exc}"))
        if not n.get("default") and not any(
            b["when"].strip().lower() in ("true", "true == true", "1 == 1") for b in n["branches"]
        ):
            issues.append(
                ValidationIssue(
                    n["id"],
                    "condition_no_default",
                    "条件节点必须配置 default，或存在恒真分支，避免所有分支均不匹配时流程悬空",
                )
            )

    # 可达性：从 start 做 DFS
    if start_id:
        reachable: set[str] = set()
        stack = [start_id]
        while stack:
            cur = stack.pop()
            if cur in reachable or cur not in ids:
                continue
            reachable.add(cur)
            stack.extend(_node_refs(ids[cur]))
        unreachable = [n["id"] for n in nodes if n["id"] not in reachable]
        for nid in unreachable:
            issues.append(ValidationIssue(nid, "unreachable_node", f"节点不可达: {nid}"))

    # 环路检测：在从 start 可达的子图上做 DFS 三色标记
    # （color map 必须独立于上面的可达性 DFS，否则节点已被染黑会漏检）
    if start_id:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {nid: WHITE for nid in reachable}

        def dfs_cycle(node_id: str, path: list[str]) -> None:
            color[node_id] = GRAY
            path.append(node_id)
            for ref in _node_refs(ids[node_id]):
                if ref not in color:
                    continue  # 不可达或缺失引用（缺失引用已单独报错）
                if color[ref] == GRAY:
                    cycle = path[path.index(ref):] + [ref]
                    issues.append(
                        ValidationIssue(ref, "cycle", f"检测到环路: {' -> '.join(cycle)}")
                    )
                elif color[ref] == WHITE:
                    dfs_cycle(ref, path)
            path.pop()
            color[node_id] = BLACK

        if color.get(start_id) == WHITE:
            dfs_cycle(start_id, [])

    # 从 start 无法抵达某个 end 不是错误（分支流程可只走一个 end），
    # 但 end 若不可达已在上面报出。
    return issues
