"""结构化 JSON 脱敏规则编译器。

规则集 JSON 形态::

    {
      "version": 1,
      "rules": [
        {"id": "mask-phone", "action": "mask", "path": "$.user.phone",
         "priority": 10, "options": {"keep_last": 4}}
      ]
    }

安全语义：

* **默认拒绝未知**：未知顶层字段、未知规则字段、未知 action、未知 option、
  类型错误的参数全部在编译期报错，拒绝整个规则集；
* **显式优先级合成**：同一字段被多条规则命中时，``priority`` 大的生效；
  优先级相同且动作不同的规则在编译期（同一路径）或运行期（通配重叠）报错，
  即 fail-closed，绝不静默二选一。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .errors import RuleCompileError, UnknownRuleError
from . import paths as pathlib

KNOWN_TOP_LEVEL = {"version", "rules"}
KNOWN_RULE_FIELDS = {"id", "action", "path", "priority", "require_match", "options"}

# 每个 action 允许的 option 名 -> 校验器（返回规范化后的值）
OptionValidator = Callable[[str, Any], Any]


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _nonneg_int(name: str, v: Any) -> int:
    if not _is_int(v) or v < 0:
        raise RuleCompileError(f"选项 {name} 必须是非负整数", details={"option": name})
    return v


def _nonempty_str(name: str, v: Any) -> str:
    if not isinstance(v, str) or not v:
        raise RuleCompileError(f"选项 {name} 必须是非空字符串", details={"option": name})
    return v


def _bool(name: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise RuleCompileError(f"选项 {name} 必须是布尔值", details={"option": name})
    return v


def _single_char(name: str, v: Any) -> str:
    v = _nonempty_str(name, v)
    if len(v) != 1:
        raise RuleCompileError(f"选项 {name} 必须是单个字符", details={"option": name})
    return v


ACTION_OPTIONS: dict[str, dict[str, tuple[OptionValidator, Any]]] = {
    # option 名 -> (校验器, 默认值)；默认值 None 表示必填（本项目全部有默认）
    "mask": {
        "keep_last": (_nonneg_int, 4),
        "mask_char": (_single_char, "*"),
    },
    "redact": {
        "replacement": (_nonempty_str, "***REDACTED***"),
    },
    "drop": {},
    "hash": {},
    "encrypt": {},
    "decrypt": {},
}


@dataclass(frozen=True)
class CompiledRule:
    id: str
    action: str
    raw_path: str
    segments: tuple[Any, ...]
    canonical_path: str
    priority: int
    require_match: bool
    options: dict[str, Any]


@dataclass(frozen=True)
class CompiledRules:
    rules: tuple[CompiledRule, ...]

    @property
    def needs_crypto(self) -> bool:
        return any(r.action in ("encrypt", "decrypt", "hash") for r in self.rules)


def compile_rules(spec: Any) -> CompiledRules:
    """把规则集 dict 编译为 CompiledRules；任何不合规处抛 RuleCompileError。"""
    if not isinstance(spec, dict):
        raise RuleCompileError("规则集必须是 JSON 对象")
    unknown_top = set(spec) - KNOWN_TOP_LEVEL
    if unknown_top:
        raise RuleCompileError(
            "规则集包含未知顶层字段（默认拒绝）",
            details={"unknown": sorted(unknown_top)},
        )
    version = spec.get("version", 1)
    if version != 1:
        raise RuleCompileError("仅支持 version=1", details={"version": version})
    raw_rules = spec.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise RuleCompileError("rules 必须是非空数组")

    compiled: list[CompiledRule] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_rules):
        rule = _compile_one(raw, index, seen_ids)
        compiled.append(rule)

    _check_path_priority_conflicts(compiled)
    return CompiledRules(rules=tuple(compiled))


def _compile_one(raw: Any, index: int, seen_ids: set[str]) -> CompiledRule:
    label = f"rules[{index}]"
    if not isinstance(raw, dict):
        raise RuleCompileError(f"{label} 必须是对象")
    unknown = set(raw) - KNOWN_RULE_FIELDS
    if unknown:
        raise RuleCompileError(
            f"{label} 包含未知字段（默认拒绝）",
            details={"unknown": sorted(unknown), "index": index},
        )

    action = raw.get("action")
    if not isinstance(action, str) or not action:
        raise RuleCompileError(f"{label}.action 必须是非空字符串", details={"index": index})
    if action not in ACTION_OPTIONS:
        raise UnknownRuleError(
            f"未知规则动作 {action!r}（默认拒绝）",
            details={"action": action, "index": index,
                     "supported": sorted(ACTION_OPTIONS)},
        )

    rule_id = raw.get("id")
    if rule_id is None:
        rule_id = f"rule-{index}"
    elif not isinstance(rule_id, str) or not rule_id:
        raise RuleCompileError(f"{label}.id 必须是非空字符串", details={"index": index})
    if rule_id in seen_ids:
        raise RuleCompileError(f"规则 id 重复：{rule_id}", details={"rule_id": rule_id})
    seen_ids.add(rule_id)

    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise RuleCompileError(f"{label}.path 必须是非空字符串",
                               details={"rule_id": rule_id})
    try:
        segments = pathlib.parse_path(raw_path)
    except ValueError as exc:
        raise RuleCompileError(
            f"规则 {rule_id} 的路径非法：{exc}", details={"rule_id": rule_id}
        ) from exc
    canonical_path = pathlib.format_segments(segments)

    priority = raw.get("priority", 0)
    if not _is_int(priority):
        raise RuleCompileError(f"规则 {rule_id} 的 priority 必须是整数",
                               details={"rule_id": rule_id})

    require_match = raw.get("require_match", False)
    if not isinstance(require_match, bool):
        raise RuleCompileError(f"规则 {rule_id} 的 require_match 必须是布尔值",
                               details={"rule_id": rule_id})

    raw_options = raw.get("options", {})
    if raw_options is None:
        raw_options = {}
    if not isinstance(raw_options, dict):
        raise RuleCompileError(f"规则 {rule_id} 的 options 必须是对象",
                               details={"rule_id": rule_id})
    options = _compile_options(action, raw_options, rule_id)

    return CompiledRule(
        id=rule_id,
        action=action,
        raw_path=raw_path,
        segments=segments,
        canonical_path=canonical_path,
        priority=priority,
        require_match=require_match,
        options=options,
    )


def _compile_options(action: str, raw: dict[str, Any], rule_id: str) -> dict[str, Any]:
    spec = ACTION_OPTIONS[action]
    unknown_opts = set(raw) - set(spec)
    if unknown_opts:
        raise RuleCompileError(
            f"规则 {rule_id}（action={action}）包含未知选项（默认拒绝）",
            details={"rule_id": rule_id, "unknown": sorted(unknown_opts)},
        )
    out: dict[str, Any] = {}
    for name, (validator, default) in spec.items():
        present = name in raw
        value = raw[name] if present else default
        try:
            out[name] = validator(name, value)
        except RuleCompileError as exc:
            exc.details["rule_id"] = rule_id
            raise
    return out


def _check_path_priority_conflicts(compiled: list[CompiledRule]) -> None:
    """同一路径模式 + 同一优先级：动作/参数不同则拒绝；完全相同则拒绝重复。"""
    groups: dict[tuple[str, int], list[CompiledRule]] = {}
    for rule in compiled:
        groups.setdefault((rule.canonical_path, rule.priority), []).append(rule)
    for (cpath, priority), rules in groups.items():
        if len(rules) < 2:
            continue
        signatures = {(r.action, tuple(sorted(r.options.items()))) for r in rules}
        ids = sorted(r.id for r in rules)
        if len(signatures) == 1:
            raise RuleCompileError(
                f"路径 {cpath} 上存在完全重复的同优先级规则",
                details={"path": cpath, "priority": priority, "rule_ids": ids},
            )
        raise RuleCompileError(
            f"路径 {cpath} 上有多条同优先级但动作不同的规则；"
            "请用 priority 字段明确先后顺序",
            details={"path": cpath, "priority": priority, "rule_ids": ids},
        )
