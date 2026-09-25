"""字段级策略引擎。

入口 :func:`apply_policy` 对输入数据（已解析的 JSON 值）做单次递归遍历，
对每个字段路径产生一条决策记录：

* ``allow``       —— 允许原值导出；
* ``deny``        —— 拒绝（字段不进入导出）；
* ``generalize``  —— 以泛化器输出替代原值；
* ``passthrough`` —— 容器本身无规则，仅作为路径向下传递（子节点逐一决策）。

根节点不产生决策记录（它没有字段路径）。

安全语义（fail closed）
----------------------
* 未匹配任何规则的**标量**字段按策略 ``default_action`` 处理，默认 deny。
  即“未知字段默认拒绝”，新增/拼错的字段不会意外泄露。
* 容器（object/array）无规则时为 passthrough，必须逐子节点决策，
  嵌套结构不存在“整体放行后内部不再检查”的旁路。
* 父节点 deny 时整个子树移除；父节点 allow 不改变子节点的独立评估
  （allow 一个对象不等于允许其全部内部字段裸出）。
* 泛化器抛错 => 该字段 deny，不回退原值。
* 数组在路径中统一规范为 ``[]`` 段，攻击者无法用具体下标绕过数组规则。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .canonical import canonical_dumps
from .errors import GeneralizerError, MdeError
from .generalizers import get_generalizer
from .models import ARRAY_SEGMENT, Policy, Rule, parse_path, render_path

# 遍历结果中“该节点被移除”的内部标记。
_REMOVED = object()
_NOT_GIVEN = object()


@dataclass
class _MatchedRule:
    rule: Rule
    alias_resolved: bool
    rank: tuple[int, int, int]  # 优先级排序键，越大越优先


class _Matcher:
    """针对单个 Policy 构建的规则索引。"""

    def __init__(self, policy: Policy):
        self._alias = policy.alias_map
        self._rules: list[tuple[Rule, list[str]]] = [
            (r, parse_path(r.path)) for r in policy.rules
        ]

    def canonical_segments(self, raw_segs: list[str]) -> list[str]:
        """把真实数据路径段规范化（别名 -> 规范名）。"""
        return [
            ARRAY_SEGMENT if s == ARRAY_SEGMENT else self._alias.get(s, s)
            for s in raw_segs
        ]

    def match(self, raw_segs: list[str], can_segs: list[str],
              purpose: str) -> _MatchedRule | None:
        """返回该路径上优先级最高的规则，无匹配返回 None。

        优先级：目的专用规则 > 通配规则；精确(非递归) > 递归；路径更长更优先。
        原始段名与规范化段名都会尝试匹配，防止别名绕过。
        """
        best: _MatchedRule | None = None
        for rule, rsegs in self._rules:
            if rule.purpose is not None and rule.purpose != purpose:
                continue
            # 非递归规则要求段数严格相等；递归规则只要求前缀（不超过）。
            if len(rsegs) > len(can_segs):
                continue
            if not rule.recursive and len(rsegs) != len(can_segs):
                continue
            kind = self._segments_match(rsegs, raw_segs, can_segs)
            if kind is None:
                continue
            exact = 1 if len(rsegs) == len(can_segs) else 0
            # rank: (目的专用?1:0, 精确长度匹配?1:0, 规则段数)
            rank = (1 if rule.purpose is not None else 0, exact, len(rsegs))
            alias_resolved = kind == "canonical" and rsegs != raw_segs[:len(rsegs)]
            if best is None or rank > best.rank:
                best = _MatchedRule(rule, alias_resolved, rank)
        return best

    @staticmethod
    def _segments_match(rsegs: list[str], raw_segs: list[str],
                        can_segs: list[str]) -> str | None:
        """判断规则段是否为数据段的匹配前缀。

        返回 "raw"/"canonical" 表示按哪种路径命中；不命中返回 None。
        非递归规则要求段数相等；递归规则只要求前缀相等（段数在调用处比较）。
        ``[]`` 段必须严格对应数组位置，不能与普通字段名混用。
        """
        raw_prefix = raw_segs[: len(rsegs)]
        can_prefix = can_segs[: len(rsegs)]
        raw_ok = True
        can_ok = True
        for i, rs in enumerate(rsegs):
            if rs == ARRAY_SEGMENT:
                if raw_prefix[i] != ARRAY_SEGMENT:
                    return None
                continue
            if raw_prefix[i] == ARRAY_SEGMENT:
                raw_ok = False
            if can_prefix[i] == ARRAY_SEGMENT:
                can_ok = False
        if raw_ok and rsegs == raw_prefix:
            return "raw"
        if can_ok and rsegs == can_prefix:
            return "canonical"
        return None


@dataclass
class ExportResult:
    """一次导出的全部产物。"""

    output: Any
    decisions: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    purpose: str = ""
    hash_salt_hex: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "decisions": self.decisions,
            "stats": self.stats,
            "purpose": self.purpose,
            "hash_salt": self.hash_salt_hex,
        }


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


def apply_policy(data: Any, policy: Policy, purpose: str,
                 hash_salt: bytes) -> ExportResult:
    """按策略处理一条记录。

    :param data: 已解析的 JSON 值（dict/list/scalar），根必须是 object 或 array。
    :param policy: 不可变策略版本（任务固定版本，见 service 层）。
    :param purpose: 导出用途，如 "analytics"。
    :param hash_salt: 任务级 HMAC 盐（用于 hash 泛化器）。
    """
    if not isinstance(purpose, str) or not purpose:
        raise MdeError("purpose must be a non-empty string")
    if not isinstance(data, (dict, list)):
        raise MdeError("export data root must be an object or array")

    matcher = _Matcher(policy)
    decisions: list[dict[str, Any]] = []
    result = ExportResult(output=None, purpose=purpose,
                          hash_salt_hex=hash_salt.hex(), decisions=decisions)

    def emit(path_segs: list[str], rec: dict[str, Any]) -> None:
        # 根节点（空路径）不是字段，不产生决策记录。
        if path_segs:
            decisions.append(rec)

    def walk(value: Any, raw: list[str], can: list[str]) -> Any:
        matched = matcher.match(raw, can, purpose)
        if _is_scalar(value):
            return _decide_scalar(value, raw, can, matched, emit, policy,
                                  hash_salt)
        if isinstance(value, dict):
            return _decide_container(value, raw, can, matched, emit, policy,
                                     hash_salt, walk, is_dict=True)
        if isinstance(value, list):
            return _decide_container(value, raw, can, matched, emit, policy,
                                     hash_salt, walk, is_dict=False)
        raise MdeError(f"unsupported value type at {render_path(can)}: "
                       f"{type(value).__name__}")

    result.output = walk(data, [], [])
    result.stats = _compute_stats(decisions)
    return result


def _compute_stats(decisions: list[dict[str, Any]]) -> dict[str, int]:
    stats = {"allow": 0, "deny": 0, "generalize": 0, "passthrough": 0}
    for d in decisions:
        stats[d["decision"]] = stats.get(d["decision"], 0) + 1
        if d.get("default_denied"):
            stats["denied_by_default"] = stats.get("denied_by_default", 0) + 1
        if d.get("by") == "generalizer_failure":
            stats["denied_by_generalizer_failure"] = (
                stats.get("denied_by_generalizer_failure", 0) + 1)
    stats["total_decisions"] = len(decisions)
    return stats


def _make_rec(can: list[str], raw: list[str], decision: str, *,
              by: str, reason: str, matched: _MatchedRule | None,
              output_value: Any = _NOT_GIVEN) -> dict[str, Any]:
    d: dict[str, Any] = {
        "path": render_path(can),
        "decision": decision,
        "by": by,
        "reason": reason,
    }
    if raw != can:
        d["input_path"] = render_path(raw)
        d["alias_resolved"] = True
    if matched is not None:
        d["matched_rule_path"] = matched.rule.path
        if matched.rule.purpose is not None:
            d["rule_purpose"] = matched.rule.purpose
        if matched.alias_resolved:
            d["resolved_via_alias"] = True
    if output_value is not _NOT_GIVEN:
        d["output_value"] = output_value
    return d


def _decide_scalar(value, raw, can, matched, emit, policy, salt):
    if matched is not None:
        rule = matched.rule
        if rule.action == "deny":
            emit(can, _make_rec(can, raw, "deny", by="rule",
                                       reason=rule.description or "denied by policy",
                                       matched=matched))
            return _REMOVED
        if rule.action == "allow":
            emit(can, _make_rec(can, raw, "allow", by="rule",
                                       reason=rule.description or "allowed by policy",
                                       matched=matched))
            return value
        # generalize
        out, ok = _run_generalizer(rule, value, salt)
        if not ok:
            emit(can, _make_rec(can, raw, "deny",
                                       by="generalizer_failure",
                                       reason=f"generalizer {rule.generalizer!r} "
                                              f"failed; fail-closed denial: {out}",
                                       matched=matched))
            return _REMOVED
        emit(can, _make_rec(can, raw, "generalize", by="rule",
                                   reason=rule.description
                                   or f"generalized via {rule.generalizer}",
                                   matched=matched, output_value=out))
        return out

    # 无规则标量：默认动作（默认 deny）——未知字段不导出。
    if policy.default_action == "allow":
        emit(can, _make_rec(can, raw, "allow", by="default",
                                   reason="no matching rule; policy default is allow",
                                   matched=None))
        return value
    rec = _make_rec(can, raw, "deny", by="default",
                    reason="no matching rule; unknown fields denied by default",
                    matched=None)
    rec["default_denied"] = True
    emit(can, rec)
    return _REMOVED


def _run_generalizer(rule: Rule, value: Any, salt: bytes) -> tuple[Any, bool]:
    """执行泛化器；任何异常都转为 (错误信息, False)，调用方据此 fail closed。"""
    try:
        fn = get_generalizer(rule.generalizer or "")
        return fn(value, rule.params_dict, salt), True
    except GeneralizerError as exc:
        return str(exc), False
    except Exception as exc:  # 泛化器的任何意外异常都不得导致原值泄露
        return f"{type(exc).__name__}: {exc}", False


def _decide_container(value, raw, can, matched, emit, policy, salt, walk,
                      is_dict):
    """处理 object 与 list（二者规则语义相同）。"""
    if matched is not None and matched.rule.action == "deny":
        emit(can, _make_rec(can, raw, "deny", by="rule",
                                   reason=matched.rule.description
                                   or "subtree denied by policy", matched=matched))
        return _REMOVED
    if matched is not None and matched.rule.action == "generalize":
        # 子树泛化仅适用于类型不敏感的泛化器（redact/replace）；
        # 类型敏感的泛化器会失败 -> fail closed，不向下继续。
        out, ok = _run_generalizer(matched.rule, value, salt)
        if not ok:
            emit(can, _make_rec(
                can, raw, "deny", by="generalizer_failure",
                reason=f"generalizer {matched.rule.generalizer!r} failed on "
                       f"subtree; fail-closed denial: {out}", matched=matched))
            return _REMOVED
        emit(can, _make_rec(can, raw, "generalize", by="rule",
                                   reason=f"subtree generalized via "
                                          f"{matched.rule.generalizer}",
                                   matched=matched, output_value=out))
        return out
    if matched is not None:  # allow：容器放行，但子节点仍逐一决策
        emit(can, _make_rec(can, raw, "allow", by="rule",
                                   reason="container allowed; children still "
                                          "evaluated individually",
                                   matched=matched))
    else:
        emit(can, _make_rec(can, raw, "passthrough", by="engine",
                                   reason="container without rule; each child "
                                          "decided separately", matched=None))

    if is_dict:
        out: Any = {}
        alias_map = policy.alias_map
        for k, v in value.items():
            if not isinstance(k, str):
                raise MdeError(f"non-string key under {render_path(can)}")
            child_can = can + [
                ARRAY_SEGMENT if k == ARRAY_SEGMENT else alias_map.get(k, k)]
            child = walk(v, raw + [k], child_can)
            if child is not _REMOVED:
                out[k] = child
        # 非根的无规则容器：若所有子节点均被拒绝，容器本身也不进入导出
        #（决策记录仍然保留，可审计“为什么空了”）。根对象始终保留。
        if not out and can:
            return _REMOVED
        return out

    # 数组在路径中以 [] 表示；所有元素共享同一规则路径。
    out = []
    for v in value:
        child = walk(v, raw + [ARRAY_SEGMENT], can + [ARRAY_SEGMENT])
        if child is not _REMOVED:
            out.append(child)
    if not out and can:
        return _REMOVED
    return out


def decisions_hash(decisions: list[dict[str, Any]]) -> str:
    """决策记录列表的 SHA-256（canonical 编码）。"""
    return hashlib.sha256(canonical_dumps(decisions)).hexdigest()


def value_hash(value: Any) -> str:
    """输出值的 SHA-256（canonical 编码）。"""
    return hashlib.sha256(canonical_dumps(value)).hexdigest()
