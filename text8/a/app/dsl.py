"""流程定义（DSL）与发布前校验。

JSON 结构示例见 demo/leave-demo.json。节点类型：start / approval / condition / end。

发布前强制校验：
1. 结构合法（字段完整、节点 ID 唯一、节点类型合法）；
2. 所有边引用的节点都存在（缺失引用拒绝）；
3. 从 start 出发所有节点可达（不可达节点拒绝）；
4. 有向图无环（环路拒绝）；
5. 条件表达式通过受限表达式白名单校验。
"""
from __future__ import annotations

import re
from typing import Any

from app.errors import ValidationError
from app.expressions import parse_expression
from app.expressions import RestrictedSyntaxError

NODE_TYPES = {"start", "approval", "condition", "end"}
APPROVAL_MODES = {"all", "any"}

_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _require(cond: bool, errors: list[str], msg: str) -> None:
    if not cond:
        errors.append(msg)


def validate_definition(definition: dict[str, Any]) -> None:
    """校验整个流程定义，失败时抛出包含全部错误的 ValidationError。"""
    errors: list[str] = []

    if not isinstance(definition, dict):
        raise ValidationError("流程定义必须是 JSON 对象")

    nodes = definition.get("nodes")
    start_node_id = definition.get("start_node")

    _require(isinstance(nodes, list) and len(nodes) > 0, errors, "nodes 必须是非空数组")
    if not isinstance(nodes, list) or not nodes:
        raise ValidationError("流程定义非法", details=errors)

    # ---- 逐节点结构校验 ----
    by_id: dict[str, dict] = {}
    starts: list[str] = []
    ends: list[str] = []

    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            errors.append(f"nodes[{i}] 必须是对象")
            continue
        nid = node.get("id")
        if not isinstance(nid, str) or not _NODE_ID_RE.match(nid):
            errors.append(f"nodes[{i}].id 非法（需匹配 {_NODE_ID_RE.pattern}）")
            continue
        if nid in by_id:
            errors.append(f"节点 ID 重复: {nid}")
            continue

        ntype = node.get("type")
        _require(ntype in NODE_TYPES, errors, f"节点 {nid}: type 必须是 {sorted(NODE_TYPES)}")

        if ntype == "start":
            starts.append(nid)
            _require(
                isinstance(node.get("next"), str), errors, f"节点 {nid}: start 必须有 next"
            )
        elif ntype == "end":
            ends.append(nid)
            _require("next" not in node, errors, f"节点 {nid}: end 不能有 next")
        elif ntype == "approval":
            _require(
                isinstance(node.get("next"), str), errors, f"节点 {nid}: approval 必须有 next"
            )
            mode = node.get("mode")
            _require(
                mode in APPROVAL_MODES,
                errors,
                f"节点 {nid}: mode 必须是 all(全签) 或 any(任签)",
            )
            assignees = node.get("assignees")
            _require(
                isinstance(assignees, list)
                and len(assignees) > 0
                and all(isinstance(a, str) and a.strip() for a in assignees),
                errors,
                f"节点 {nid}: assignees 必须是非空字符串数组",
            )
            if isinstance(assignees, list) and len(set(assignees)) != len(assignees):
                errors.append(f"节点 {nid}: assignees 不能重复")
            timeout = node.get("timeout")
            if timeout is not None:
                _validate_timeout(nid, timeout, errors)
        elif ntype == "condition":
            branches = node.get("branches")
            _require(
                isinstance(branches, list) and len(branches) > 0,
                errors,
                f"节点 {nid}: condition 必须有非空 branches",
            )
            if isinstance(branches, list):
                for j, br in enumerate(branches):
                    if not isinstance(br, dict):
                        errors.append(f"节点 {nid}.branches[{j}] 必须是对象")
                        continue
                    when = br.get("when")
                    if isinstance(when, str) and when.strip():
                        try:
                            parse_expression(when)
                        except RestrictedSyntaxError as exc:
                            errors.append(f"节点 {nid}.branches[{j}].when 非法: {exc}")
                    else:
                        errors.append(f"节点 {nid}.branches[{j}].when 必须是非空字符串")
                    _require(
                        isinstance(br.get("next"), str),
                        errors,
                        f"节点 {nid}.branches[{j}] 必须有 next",
                    )
            _require(
                isinstance(node.get("default"), str),
                errors,
                f"节点 {nid}: condition 必须有 default 分支",
            )
            _require("next" not in node, errors, f"节点 {nid}: condition 使用 branches/default，不能有 next")

        by_id[nid] = node

    _require(len(starts) == 1, errors, f"必须恰好有一个 start 节点（当前 {len(starts)} 个）")
    _require(len(ends) >= 1, errors, "必须至少有一个 end 节点")

    if errors:
        raise ValidationError("流程定义非法", details=errors)

    # ---- 引用校验 ----
    _require(
        isinstance(start_node_id, str) and start_node_id in by_id,
        errors,
        f"start_node 必须指向已存在的节点（当前: {start_node_id!r}）",
    )

    edges: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, node in by_id.items():
        ntype = node["type"]
        if ntype in ("start", "approval"):
            target = node.get("next")
            if target not in by_id:
                errors.append(f"节点 {nid}.next 引用了不存在的节点: {target!r}")
            else:
                edges[nid].append(target)
        elif ntype == "condition":
            for br in node["branches"]:
                target = br.get("next")
                if target not in by_id:
                    errors.append(
                        f"节点 {nid} 分支引用了不存在的节点: {target!r}"
                    )
                else:
                    edges[nid].append(target)
            default = node.get("default")
            if default not in by_id:
                errors.append(f"节点 {nid}.default 引用了不存在的节点: {default!r}")
            else:
                edges[nid].append(default)

    if errors:
        raise ValidationError("流程引用非法", details=errors)

    # ---- 可达性 ----
    reachable: set[str] = set()
    stack = [start_node_id]
    while stack:
        cur = stack.pop()
        if cur in reachable:
            continue
        reachable.add(cur)
        stack.extend(edges[cur])

    unreachable = [nid for nid in by_id if nid not in reachable]
    if unreachable:
        errors.append(f"存在不可达节点: {sorted(unreachable)}")

    # end 节点不应有出边（结构上已禁止 next），这里确认所有路径最终可达某个 end：
    # 因为无环且 end 是唯一无出边类型，检查每个汇合路径 —— 逐节点确认：
    # 非 end 节点必然有出边（上面已校验），无环图中沿边走必然终止于 end。
    # ---- 环路检测（DFS 三色） ----
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in by_id}
    cycle: list[str] = []

    def dfs(nid: str, path: list[str]) -> bool:
        color[nid] = GRAY
        path.append(nid)
        for tgt in edges[nid]:
            if color[tgt] == GRAY:
                cycle.extend(path[path.index(tgt):] + [tgt])
                return True
            if color[tgt] == WHITE and dfs(tgt, path):
                return True
        path.pop()
        color[nid] = BLACK
        return False

    for nid in by_id:
        if color[nid] == WHITE and dfs(nid, []):
            errors.append(f"存在环路: {' -> '.join(cycle)}")
            break

    if errors:
        raise ValidationError("流程定义非法", details=errors)


def _validate_timeout(nid: str, timeout: Any, errors: list[str]) -> None:
    if not isinstance(timeout, dict):
        errors.append(f"节点 {nid}: timeout 必须是对象")
        return
    seconds = timeout.get("seconds")
    _require(
        isinstance(seconds, int) and not isinstance(seconds, bool) and seconds > 0,
        errors,
        f"节点 {nid}: timeout.seconds 必须是正整数（秒）",
    )
    targets = timeout.get("targets")
    _require(
        isinstance(targets, list)
        and len(targets) > 0
        and all(isinstance(t, str) and t.strip() for t in targets),
        errors,
        f"节点 {nid}: timeout.targets 必须是非空字符串数组",
    )


def out_targets(node: dict[str, Any]) -> list[str]:
    """返回节点的全部出边目标（供引擎/分析使用）。"""
    if node["type"] in ("start", "approval"):
        return [node["next"]]
    if node["type"] == "condition":
        return [b["next"] for b in node["branches"]] + [node["default"]]
    return []


def node_index(definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in definition["nodes"]}
