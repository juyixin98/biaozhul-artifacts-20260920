"""字段级导出策略引擎。

对每条记录递归求值，输出（泛化后的）最小披露结果与逐字段决策记录。

设计要点：
- 每个叶子字段都产生一条可核验决策（allow / deny / generalize），
  容器节点产生 structural 决策；任何被丢弃的字段都有拒绝原因。
- 规则解析顺序：精确别名 → 精确字段规则 → 最近祖先（子树）规则 → 用途默认。
  策略发布时已保证同用途规则互不为前缀，祖先规则对整棵子树语义一致。
- 数组形状（``[]``）只匹配列表元素；记录实际类型与形状不符时规则不命中，
  回落到默认拒绝 —— 嵌套结构无法靠"把对象塞进数组/把数组换成对象"旁路。
- 路径段对 ``. [ ] %`` 做百分号编码，字面键不可能冒充嵌套路径。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import DECISION_SCHEMA
from .paths import ARRAY, encode_segment, instance_tokens, join_tokens
from .policy import (
    ALLOW,
    DENY,
    GENERALIZE,
    CompiledPolicy,
    CompiledPurpose,
)
from .signing import canonical, sha256_hex
from .transforms import TransformContext, TransformError, apply_transform

DROP = object()  # 哨兵：该节点不出现在输出中


@dataclass
class RuleHit:
    action: str                 # allow | deny | generalize
    rule_tokens: Tuple[str, ...]
    transform: Any = None       # TransformRule（仅 generalize）
    via_alias: Optional[Tuple[str, ...]] = None


@dataclass
class EngineConfig:
    purpose: str
    policy: CompiledPolicy
    ctx: TransformContext


@dataclass
class _Walk:
    cfg: EngineConfig
    decisions: List[dict] = field(default_factory=list)
    record_index: int = 0

    @property
    def purpose_view(self) -> CompiledPurpose:
        return self.cfg.policy.purpose(self.cfg.purpose)

    # ---- 规则查找 ----------------------------------------------------------

    def _exact_rule(
        self, shape: Tuple[str, ...]
    ) -> Optional[RuleHit]:
        view = self.purpose_view
        if shape in self.cfg.policy.aliases:
            dst = self.cfg.policy.aliases[shape]
            if dst in view.generalize:
                return RuleHit(GENERALIZE, dst, view.generalize[dst], via_alias=shape)
            if dst in view.allow:
                return RuleHit(ALLOW, dst, via_alias=shape)
            if dst in view.deny:
                return RuleHit(DENY, dst, via_alias=shape)
            # 别名目标在别的用途中存在、本用途未配置：不命中（回落默认）
            return None
        if shape in view.generalize:
            return RuleHit(GENERALIZE, shape, view.generalize[shape])
        if shape in view.allow:
            return RuleHit(ALLOW, shape)
        if shape in view.deny:
            return RuleHit(DENY, shape)
        return None

    def _has_rules_beneath(self, shape: Tuple[str, ...]) -> bool:
        """该形状之下（严格后代）是否配置过任何规则或别名目标。"""
        view = self.purpose_view
        paths = (
            list(view.generalize) + list(view.allow) + list(view.deny)
            + list(self.cfg.policy.aliases.values())
        )
        return any(len(t) > len(shape) and self._shape_prefix(shape, t) for t in paths)

    @staticmethod
    def _shape_prefix(rule: Tuple[str, ...], shape: Tuple[str, ...]) -> bool:
        """规则形状是否为当前形状的祖先前缀（[] 可对应 [k]）。"""
        if len(rule) > len(shape):
            return False
        for r, s in zip(rule, shape):
            if r == s:
                continue
            if r == ARRAY and isinstance(s, str) and s.startswith("[") and s.endswith("]"):
                continue
            return False
        return True

    def _ancestor_rule(
        self, shape: Tuple[str, ...]
    ) -> Optional[RuleHit]:
        """最近的祖先子树规则（最长前缀）。"""
        view = self.purpose_view
        best: Optional[RuleHit] = None
        candidates = (
            [(GENERALIZE, t, tr) for t, tr in view.generalize.items()]
            + [(ALLOW, t, None) for t in view.allow]
            + [(DENY, t, None) for t in view.deny]
        )
        for action, tokens, tr in candidates:
            if len(tokens) >= len(shape):
                continue  # 精确命中已在 _exact_rule 处理
            if self._shape_prefix(tokens, shape):
                if best is None or len(tokens) > len(best.rule_tokens):
                    best = RuleHit(action, tokens, tr)
        # 注意：祖先 generalize（如 users[]）作用在标量叶子上；容器遇到它时
        # 不执行泛化，继续下钻，由具体叶子命中（[] 与具体 [k] 等长，属精确命中）。
        return best

    # ---- 决策记录 ----------------------------------------------------------

    def _decide(
        self,
        *,
        shape: Tuple[str, ...],
        indices: Dict[int, int],
        structural: bool,
        value: Any,
        action: str,
        decision: str,
        rule: Optional[RuleHit],
        reason: str,
        output_value: Any = None,
        warning: Optional[str] = None,
    ) -> None:
        inst = instance_tokens(shape, indices) if shape else []
        value_type = _json_type(value)
        entry: Dict[str, Any] = {
            "schema": DECISION_SCHEMA,
            "record_index": self.record_index,
            "path": join_tokens(inst),
            "shape_path": join_tokens(shape),
            "structural": structural,
            "value_type": value_type,
            "action": action,
            "decision": decision,
            "reason": reason,
            "warning": warning,
            "via_alias": join_tokens(rule.via_alias) if rule and rule.via_alias else None,
            "rule_path": join_tokens(rule.rule_tokens) if rule else None,
        }
        if not structural:
            entry["input_digest"] = sha256_hex(canonical(value))
            if decision in (ALLOW, GENERALIZE):
                entry["output_digest"] = sha256_hex(canonical(output_value))
        self.decisions.append(entry)

    # ---- 递归 --------------------------------------------------------------

    def walk(
        self,
        value: Any,
        shape: Tuple[str, ...],
        indices: Dict[int, int],
        inherited: Optional[RuleHit],
        original_empty_container: bool,
    ) -> Any:
        if isinstance(value, dict):
            return self._walk_dict(value, shape, indices, inherited, original_empty_container)
        if isinstance(value, list):
            return self._walk_list(value, shape, indices, inherited, original_empty_container)
        return self._walk_scalar(value, shape, indices, inherited)

    def _walk_dict(
        self, value: dict, shape, indices, inherited, original_empty
    ) -> Any:
        exact = self._exact_rule(shape) if shape != () else None
        hit = exact or inherited

        # 容器上的硬决策
        if hit is not None and hit.action == DENY:
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action=DENY, decision=DENY, rule=hit,
                reason="exact_rule" if exact else "denied_by_ancestor_rule",
            )
            return DROP
        if exact is not None and exact.action == GENERALIZE:
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action=GENERALIZE, decision=DENY, rule=exact,
                reason="generalize_rule_requires_scalar",
                warning="泛化规则命中对象容器，字段被移除（请在策略中细化到标量）",
            )
            return DROP

        child_inherited = hit if (hit and hit.action == ALLOW) else inherited
        if original_empty and not (
            child_inherited is not None or self._has_rules_beneath(shape)
        ):
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action=DENY, decision=DENY, rule=None,
                reason="empty_unknown_container_denied",
            )
            return DROP
        if original_empty:
            reason = "empty_container_kept"
        else:
            reason = "container_recursed"
        self._decide(
            shape=shape, indices=indices, structural=True, value=value,
            action=hit.action if hit else "none",
            decision=ALLOW, rule=hit, reason=reason,
        )

        out: Dict[str, Any] = {}
        surviving = 0
        for k, v in value.items():
            if not isinstance(k, str):
                # JSON 对象键恒为字符串；非字符串键不可能来自 JSON 输入
                self._decide(
                    shape=shape + (encode_segment(str(k)),), indices=indices,
                    structural=True, value=v, action="deny", decision=DENY,
                    rule=None, reason="non_string_key",
                )
                continue
            child_shape = shape + (encode_segment(k),)
            child = self.walk(v, child_shape, indices, child_inherited, isinstance(v, (dict, list)) and len(v) == 0)
            if child is not DROP:
                out[k] = child
                surviving += 1

        if not original_empty and surviving == 0:
            # 非空容器在策略下没有任何可输出字段：整体移除，避免空壳泄露存在性
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action="deny", decision=DENY, rule=None,
                reason="container_dropped_no_surviving_fields",
            )
            return DROP
        return out

    def _walk_list(
        self, value: list, shape, indices, inherited, original_empty
    ) -> Any:
        # 列表容器本身只可能被精确规则/祖先规则整体允许或拒绝；
        # 作用于元素的规则（形状以 [] 结尾）不匹配列表容器本身。
        exact = self._exact_rule(shape) if shape != () else None
        hit = exact or inherited
        if hit is not None and hit.action == DENY:
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action=DENY, decision=DENY, rule=hit,
                reason="exact_rule" if exact else "denied_by_ancestor_rule",
            )
            return DROP
        if exact is not None and exact.action == GENERALIZE:
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action=GENERALIZE, decision=DENY, rule=exact,
                reason="generalize_rule_requires_scalar",
                warning="泛化规则命中数组容器，字段被移除（请细化到 [] 元素）",
            )
            return DROP

        child_inherited = hit if (hit and hit.action == ALLOW) else inherited
        if original_empty and not (
            child_inherited is not None or self._has_rules_beneath(shape)
        ):
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action=DENY, decision=DENY, rule=None,
                reason="empty_unknown_container_denied",
            )
            return DROP
        self._decide(
            shape=shape, indices=indices, structural=True, value=value,
            action=hit.action if hit else "none",
            decision=ALLOW, rule=hit,
            reason="empty_container_kept" if original_empty else "container_recursed",
        )

        depth = len([t for t in shape if t == ARRAY])
        out: List[Any] = []
        for i, v in enumerate(value):
            idx = dict(indices)
            idx[depth] = i
            elem_shape = shape + (ARRAY,)
            child = self.walk(v, elem_shape, idx, child_inherited,
                              isinstance(v, (dict, list)) and len(v) == 0)
            if child is not DROP:
                out.append(child)

        if not original_empty and len(out) == 0:
            self._decide(
                shape=shape, indices=indices, structural=True, value=value,
                action="deny", decision=DENY, rule=None,
                reason="container_dropped_no_surviving_fields",
            )
            return DROP
        return out

    def _walk_scalar(self, value, shape, indices, inherited) -> Any:
        exact = self._exact_rule(shape) if shape != () else None
        hit = exact or inherited or self._ancestor_rule(shape)
        view = self.purpose_view

        if hit is None:
            action, decision, reason = "none", view.default, (
                "default_allow" if view.default == ALLOW else "default_deny"
            )
            if view.default == ALLOW:
                self._decide(
                    shape=shape, indices=indices, structural=False, value=value,
                    action=ALLOW, decision=ALLOW, rule=None, reason=reason,
                    output_value=value,
                )
                return value
            self._decide(
                shape=shape, indices=indices, structural=False, value=value,
                action=DENY, decision=DENY, rule=None, reason=reason,
            )
            return DROP

        if hit.action == ALLOW:
            self._decide(
                shape=shape, indices=indices, structural=False, value=value,
                action=ALLOW, decision=ALLOW, rule=hit,
                reason="exact_rule" if exact else "subtree_allow",
                output_value=value,
            )
            return value

        if hit.action == DENY:
            self._decide(
                shape=shape, indices=indices, structural=False, value=value,
                action=DENY, decision=DENY, rule=hit,
                reason="exact_rule" if exact else "denied_by_ancestor_rule",
            )
            return DROP

        # generalize（只可能在标量上到达；容器情形已在上游拒绝）
        tr = hit.transform
        try:
            gen_value = apply_transform(tr.name, value, tr.params, self.cfg.ctx)
        except TransformError as e:
            self._decide(
                shape=shape, indices=indices, structural=False, value=value,
                action=GENERALIZE, decision=DENY, rule=hit,
                reason="transform_failed", warning=str(e),
            )
            return DROP
        self._decide(
            shape=shape, indices=indices, structural=False, value=value,
            action=GENERALIZE, decision=GENERALIZE, rule=hit,
            reason="exact_rule" if exact else "subtree_generalize",
            output_value=gen_value,
        )
        return gen_value


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def export_records(
    records: List[dict],
    compiled: CompiledPolicy,
    purpose: str,
    ctx: TransformContext,
) -> Tuple[List[Any], List[dict]]:
    """对一批记录执行策略，返回 (输出记录, 决策记录)。"""
    if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
        raise ValueError("records 必须是对象数组")
    # 提前校验用途存在，给出清晰错误
    compiled.purpose(purpose)
    walker = _Walk(cfg=EngineConfig(purpose=purpose, policy=compiled, ctx=ctx))
    outputs: List[Any] = []
    for i, record in enumerate(records):
        walker.record_index = i
        out = walker.walk(record, (), {}, inherited=None, original_empty_container=False)
        outputs.append(out if out is not DROP else {})
    return outputs, walker.decisions
