"""脱敏执行引擎。

对编译后的规则集与一份 JSON 文档：

1. 计算每条规则的所有匹配踪迹；
2. ``require_match`` 规则未命中即报 MissingFieldError（缺失字段用例）；
3. 自顶向下递归合成：同一字段被多条规则命中时 priority 高者生效，
   同优先级不同规则命中同一位置即 RuleConflictError（fail-closed）；
4. 返回脱敏后的文档（深拷贝，不修改入参）与统计信息。

错误消息只含规则 id / 路径，绝不含字段值。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from .crypto import CryptoProvider
from .errors import MissingFieldError, RuleConflictError, TransformError
from .logging_utils import sensitive_scope
from .paths import find_matches, format_segments
from .rules import CompiledRule, CompiledRules

_DROP = object()  # 内部哨兵：丢弃该字段


@dataclass
class ApplyResult:
    document: Any
    stats: dict[str, Any] = field(default_factory=dict)


def apply_rules(
    compiled: CompiledRules,
    document: Any,
    crypto: CryptoProvider | None = None,
) -> ApplyResult:
    if compiled.needs_crypto and crypto is None:
        raise TransformError(
            "规则集需要密码学提供方（hash/encrypt/decrypt），但未提供 CryptoProvider"
        )
    _validate_jsonish(document)
    doc = copy.deepcopy(document)

    # rule_id -> 匹配踪迹集合
    matches: dict[str, list[tuple]] = {}
    for rule in compiled.rules:
        trails = find_matches(doc, rule.segments)
        matches[rule.id] = trails
        if rule.require_match and not trails:
            raise MissingFieldError(
                f"规则 {rule.id} 标记 require_match，但文档中无匹配字段",
                details={"rule_id": rule.id, "path": format_segments(rule.segments)},
            )

    # 每个踪迹上按规则收集（同一规则不会在同一踪迹上出现两次）
    by_trail: dict[tuple, list[CompiledRule]] = {}
    for rule in compiled.rules:
        for trail in matches[rule.id]:
            by_trail.setdefault(trail, []).append(rule)

    # 预计算：每个踪迹子树中直接命中的最高优先级
    subtree_best: dict[tuple, int | None] = {}
    trails = list(by_trail.keys())
    for trail in trails:
        best: int | None = None
        for t in trails:
            if t[: len(trail)] == trail:
                for r in by_trail[t]:
                    if best is None or r.priority > best:
                        best = r.priority
        subtree_best[trail] = best

    per_rule_counts = {r.id: 0 for r in compiled.rules}

    with sensitive_scope(document):
        root = _visit(doc, (), (), by_trail, subtree_best, crypto, per_rule_counts)

    result_doc: Any = None if root is _DROP else root

    stats = {
        "matched_fields": sum(per_rule_counts.values()),
        "rules": [
            {
                "id": r.id,
                "action": r.action,
                "path": format_segments(r.segments),
                "matched": len(matches[r.id]),
                "applied": per_rule_counts[r.id],
            }
            for r in compiled.rules
        ],
    }
    return ApplyResult(document=result_doc, stats=stats)


def _visit(
    node: Any,
    trail: tuple,
    inherited: tuple[CompiledRule, ...],
    by_trail: dict[tuple, list[CompiledRule]],
    subtree_best: dict[tuple, int | None],
    crypto: CryptoProvider | None,
    counts: dict[str, int],
) -> Any:
    """统一的规则合成递归。

    * 当前节点的候选规则 = 直接命中 + 祖先继承下来的规则；
    * 最高优先级者在该位置生效；同优先级不同规则 = RuleConflictError；
    * 容器节点不直接整体替换（drop 除外）：胜者沿子树"流"到标量叶节点；
    * 子树若含更高优先级规则，则该子树脱离继承自行处理（"刺穿"）；
    * 同优先级的不同规则作用域重叠 = fail-closed。
    """
    candidates = list(inherited)
    direct = by_trail.get(trail, ())
    for r in direct:
        if all(r.id != x.id for x in candidates):
            candidates.append(r)

    winner: CompiledRule | None = None
    if candidates:
        top_prio = max(r.priority for r in candidates)
        top = [r for r in candidates if r.priority == top_prio]
        if len({r.id for r in top}) > 1:
            raise RuleConflictError(
                "多条同优先级规则命中同一字段且动作无法合并；"
                "请用 priority 明确唯一胜者",
                details={"rule_ids": sorted(r.id for r in top),
                         "trail": _safe_trail(trail)},
            )
        winner = top[0]

    if winner is None:
        return _rebuild(node, trail, (), by_trail, subtree_best, crypto, counts)

    if isinstance(node, (dict, list)):
        items = node.items() if isinstance(node, dict) else enumerate(node)
        rebuilt: dict[Any, Any] = {} if isinstance(node, dict) else []
        for key, child in items:
            child_trail = trail + (key,)
            child_best = _subtree_best(child_trail, by_trail, subtree_best)
            if child_best is not None and child_best > winner.priority:
                # 高优先级后代刺穿：脱离当前继承，子树自行合成
                result = _visit(child, child_trail, (), by_trail, subtree_best,
                                crypto, counts)
            elif child_best == winner.priority:
                # 同优先级：同一条递归/通配规则不算冲突，不同规则重叠即冲突
                descendant_ids = _direct_ids_in_subtree(child_trail, by_trail)
                if descendant_ids - {winner.id}:
                    raise RuleConflictError(
                        "同优先级规则作用域重叠；请用 priority 明确优先级",
                        details={
                            "ancestor_rule_id": winner.id,
                            "descendant_rule_ids": sorted(descendant_ids - {winner.id}),
                            "trail": _safe_trail(child_trail),
                        },
                    )
                result = _visit(child, child_trail, (winner,), by_trail,
                                subtree_best, crypto, counts)
            else:
                result = _visit(child, child_trail, (winner,), by_trail,
                                subtree_best, crypto, counts)
            # 标量叶节点上的 drop 直接删除；容器 drop 仅在没有高优先级子树
            # 时才已经提前返回 _DROP
            if result is _DROP:
                continue
            if isinstance(rebuilt, dict):
                rebuilt[key] = result
            else:
                rebuilt.append(result)

        # drop 在容器上整体移除；若存在被高优先级保留的子树，则保留容器骨架
        if winner.action == "drop" and not _has_escaped_descendants(
            trail, winner.priority, by_trail
        ):
            return _DROP
        return rebuilt

    counts[winner.id] += 1
    return _apply_action(winner, node, trail, crypto)


def _rebuild(
    node: Any,
    trail: tuple,
    inherited: tuple[CompiledRule, ...],
    by_trail: dict[tuple, list[CompiledRule]],
    subtree_best: dict[tuple, int | None],
    crypto: CryptoProvider | None,
    counts: dict[str, int],
) -> Any:
    """遍历容器子节点，跳过被 drop 的结果。"""
    if isinstance(node, dict):
        out: dict[Any, Any] = {}
        for k, v in node.items():
            result = _visit(v, trail + (k,), inherited, by_trail, subtree_best,
                            crypto, counts)
            if result is not _DROP:
                out[k] = result
        return out
    if isinstance(node, list):
        out_list: list[Any] = []
        for i, v in enumerate(node):
            result = _visit(v, trail + (i,), inherited, by_trail, subtree_best,
                            crypto, counts)
            if result is not _DROP:
                out_list.append(result)
        return out_list
    return node


def _subtree_best(
    trail: tuple,
    by_trail: dict[tuple, list[CompiledRule]],
    cache: dict[tuple, int | None],
) -> int | None:
    if trail in cache:
        return cache[trail]
    best: int | None = None
    for t, rules in by_trail.items():
        if t[: len(trail)] == trail:
            for r in rules:
                if best is None or r.priority > best:
                    best = r.priority
    cache[trail] = best
    return best


def _direct_ids_in_subtree(
    trail: tuple, by_trail: dict[tuple, list[CompiledRule]]
) -> set[str]:
    ids: set[str] = set()
    for t, rules in by_trail.items():
        if t[: len(trail)] == trail:
            ids.update(r.id for r in rules)
    return ids


def _has_escaped_descendants(
    trail: tuple, prio: int, by_trail: dict[tuple, list[CompiledRule]]
) -> bool:
    for t, rules in by_trail.items():
        if len(t) > len(trail) and t[: len(trail)] == trail:
            if any(r.priority > prio for r in rules):
                return True
    return False


def _safe_trail(trail: tuple) -> list:
    """踪迹中只有键名/下标，属于结构信息，可安全输出。"""
    return list(trail)


def _apply_action(
    rule: CompiledRule, value: Any, trail: tuple, crypto: CryptoProvider | None
) -> Any:
    action = rule.action
    try:
        if action == "mask":
            return _mask(value, rule)
        if action == "redact":
            return rule.options["replacement"]
        if action == "drop":
            return _DROP
        if action == "hash":
            assert crypto is not None
            return crypto.hash_value(_scalar_to_text(value, rule))
        if action == "encrypt":
            assert crypto is not None
            return crypto.encrypt(_scalar_to_text(value, rule))
        if action == "decrypt":
            assert crypto is not None
            return crypto.decrypt(_scalar_to_text(value, rule))
    except TransformError as exc:
        exc.details.setdefault("rule_id", rule.id)
        exc.details.setdefault("trail", _safe_trail(trail))
        raise
    except Exception as exc:  # noqa: BLE001 - 兜底，防止第三方库异常携带值
        raise TransformError(
            f"规则 {rule.id} 执行失败：{type(exc).__name__}",
            details={"rule_id": rule.id, "trail": _safe_trail(trail)},
        ) from exc
    # 编译期已保证 action 合法
    raise TransformError(f"规则 {rule.id} 使用了未实现的动作",
                         details={"rule_id": rule.id})


def _mask(value: Any, rule: CompiledRule) -> str:
    if not isinstance(value, str):
        raise TransformError(
            f"规则 {rule.id} 的 mask 动作仅支持字符串值",
            details={"rule_id": rule.id},
        )
    keep = rule.options["keep_last"]
    char = rule.options["mask_char"]
    # Unicode：按码点计数，emoji 等组合序列按字符序列处理
    if keep == 0:
        visible = ""
        kept = 0
    else:
        visible = value[-keep:] if keep < len(value) else value
        kept = min(keep, len(value))
    return char * (len(value) - kept) + visible


def _scalar_to_text(value: Any, rule: CompiledRule) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (dict, list)):
        raise TransformError(
            f"规则 {rule.id} 的 {rule.action} 动作仅支持标量值",
            details={"rule_id": rule.id},
        )
    # bool 是 int 的子类：统一用 JSON 风格小写
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _validate_jsonish(node: Any, depth: int = 0) -> None:
    if depth > 1000:
        raise TransformError("文档嵌套深度超过 1000，拒绝处理")
    if isinstance(node, dict):
        for k, v in node.items():
            if not isinstance(k, str):
                raise TransformError("文档对象键必须是字符串")
            _validate_jsonish(v, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _validate_jsonish(v, depth + 1)
    elif isinstance(node, (str, int, float, bool)) or node is None:
        if isinstance(node, float) and (node != node or node in (float("inf"), float("-inf"))):
            raise TransformError("文档包含 NaN/Infinity，不是合法 JSON 值")
        return
    else:
        raise TransformError("文档包含不可 JSON 序列化的值")
