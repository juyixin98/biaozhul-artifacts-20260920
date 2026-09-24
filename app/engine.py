"""策略引擎：结构校验 + 显式拒绝优先（deny-overrides）求值。

决策规则（与规则书写顺序无关的集合语义）
----------------------------------------
1. 条件为 TRUE 的 deny 规则生效 => 最终 DENY（显式拒绝优先于一切 allow）；
2. 否则，只要有条件为 TRUE 的 allow 规则 => ALLOW；
3. 否则（条件 FALSE / UNKNOWN，没有任何规则生效）=> DENY（默认拒绝）。

属性缺失使条件成为 UNKNOWN（见 :mod:`app.conditions`），
UNKNOWN 不触发任何规则，因此按拒绝处理。

输出中的规则集合全部按规则 id 排序后返回，保证：
* 同样的策略内容打乱顺序后，decision 与集合结果完全一致；
* ``minimal_relevant_rules`` 是一个最小代表性集合
  （DENY 时任取一条生效的 deny 即可单独推出拒绝；ALLOW 时任取一条生效的
  allow 即可单独推出放行），``matched_rules`` 给出全部生效规则，
  ``conflicting_allow_rules`` 给出被压制的冲突 allow。
"""
from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple

from .conditions import Condition
from .errors import MAX_POLICIES, MAX_RULES, PolicyError

ALLOW = "allow"
DENY = "deny"

DECISION_ALLOW = "ALLOW"
DECISION_DENY = "DENY"


class PolicySet:
    """校验并编译后的策略集。"""

    def __init__(self, raw: Any) -> None:
        self.errors: List[str] = []
        # (rule_id, policy_id, effect, condition)
        self.rules: List[Tuple[str, str, str, Condition]] = []
        try:
            self._parse(raw)
        except PolicyError as exc:
            self.errors.extend(exc.details)

    def _parse(self, raw: Any) -> None:
        if not isinstance(raw, list):
            raise PolicyError("策略集必须是 policy 对象数组")
        if len(raw) == 0:
            raise PolicyError("策略集不能为空")
        if len(raw) > MAX_POLICIES:
            raise PolicyError(f"策略数量超过上限 {MAX_POLICIES}")

        errors: List[str] = []
        policy_ids: Set[str] = set()
        rule_ids: Set[str] = set()

        for p_idx, policy in enumerate(raw):
            p_path = f"$[{p_idx}]"
            if not isinstance(policy, dict):
                errors.append(f"{p_path}: policy 必须是对象")
                continue
            pid = policy.get("id")
            if not isinstance(pid, str) or not pid.strip():
                errors.append(f"{p_path}: policy.id 必须是非空字符串")
                continue
            if pid in policy_ids:
                errors.append(f"{p_path}: policy.id 重复: {pid!r}")
                continue
            policy_ids.add(pid)

            rules = policy.get("rules")
            if not isinstance(rules, list) or not rules:
                errors.append(f"{p_path}({pid}): rules 必须是非空数组")
                continue

            for r_idx, rule in enumerate(rules):
                r_path = f"{p_path}.rules[{r_idx}]"
                if not isinstance(rule, dict):
                    errors.append(f"{r_path}: rule 必须是对象")
                    continue
                rid = rule.get("id")
                if not isinstance(rid, str) or not rid.strip():
                    errors.append(f"{r_path}: rule.id 必须是非空字符串")
                    continue
                if rid in rule_ids:
                    errors.append(f"{r_path}: rule.id 全局重复: {rid!r}")
                    continue
                effect = rule.get("effect")
                if effect not in (ALLOW, DENY):
                    errors.append(
                        f"{r_path}({rid}): effect 必须是 allow 或 deny，得到 {effect!r}"
                    )
                    continue
                if "condition" not in rule:
                    errors.append(f"{r_path}({rid}): 缺少 condition")
                    continue
                cond = Condition(rule["condition"], path=f"{r_path}({rid}).condition")
                cond.validate()
                if cond.errors:
                    errors.extend(cond.errors)
                    continue
                rule_ids.add(rid)
                self.rules.append((rid, pid, effect, cond))

        if len(self.rules) > MAX_RULES:
            errors.append(f"$: 规则总数超过上限 {MAX_RULES}")
        if errors:
            raise PolicyError(errors)

    def validate(self) -> None:
        if self.errors:
            raise PolicyError(self.errors)


def evaluate_policy_set(
    policy_set: PolicySet,
    subject: Dict[str, Any],
    resource: Dict[str, Any],
) -> Dict[str, Any]:
    """对单个访问请求求值，返回决定与最小相关规则集合。"""
    policy_set.validate()
    if not isinstance(subject, dict) or not isinstance(resource, dict):
        raise PolicyError("subject 与 resource 必须是对象")

    rule_results: List[Dict[str, Any]] = []
    fired_allow: Set[str] = set()
    fired_deny: Set[str] = set()
    unknown_count = 0
    not_applicable_count = 0

    for rid, pid, effect, cond in policy_set.rules:
        value = cond.evaluate(subject, resource)
        if value is True:
            fired = True
            tri = "true"
            (fired_deny if effect == DENY else fired_allow).add(rid)
        elif value is False:
            fired = False
            tri = "false"
            not_applicable_count += 1
        else:
            fired = False
            tri = "unknown"
            unknown_count += 1
        rule_results.append(
            {
                "rule_id": rid,
                "policy_id": pid,
                "effect": effect,
                "condition_value": tri,
                "fired": fired,
            }
        )

    # ---- 组合：deny-overrides，集合语义（顺序无关） ----
    if fired_deny:
        decision = DECISION_DENY
        reason = "explicit_deny"
        matched = fired_deny
        conflicting = fired_allow  # 同时生效但被压制的 allow
    elif fired_allow:
        decision = DECISION_ALLOW
        reason = "explicit_allow"
        matched = fired_allow
        conflicting = set()
    else:
        decision = DECISION_DENY
        reason = "no_applicable_rule"  # 全部 false/unknown => 默认拒绝
        matched = set()
        conflicting = set()

    sorted_matched = sorted(matched)
    minimal = sorted_matched[:1]  # 最小代表性集合：字典序最小的一条生效规则

    return {
        "decision": decision,
        "reason": reason,
        "combining_algorithm": "deny-overrides",
        "minimal_relevant_rules": minimal,
        "matched_rules": sorted_matched,
        "conflicting_allow_rules": sorted(conflicting),
        "counts": {
            "rules_total": len(policy_set.rules),
            "fired_allow": len(fired_allow),
            "fired_deny": len(fired_deny),
            "not_applicable": not_applicable_count,
            "indeterminate": unknown_count,
        },
        "rule_results": sorted(rule_results, key=lambda r: r["rule_id"]),
    }
