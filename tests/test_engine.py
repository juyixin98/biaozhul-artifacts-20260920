"""策略引擎测试：deny-overrides、顺序无关（全排列）、多策略冲突、最小规则集。"""
from __future__ import annotations

import itertools
import random

import pytest

from app.engine import DECISION_ALLOW, DECISION_DENY, PolicySet, evaluate_policy_set
from app.errors import PolicyError


# ---- 测试用规则构件 -------------------------------------------------------

def rule(rid, effect, condition):
    return {"id": rid, "effect": effect, "condition": condition}


def eq(path, value):
    return {"op": "eq", "args": [{"attr": path}, {"literal": value}]}


def in_set(path, values):
    # 集合属性与给定集合相交即代表“属性集合包含其中某个值”
    return {"op": "intersects", "args": [{"attr": path}, {"literal": values}]}


def policy(pid, rules):
    return {"id": pid, "rules": rules}


def compile_and_eval(policies, subject, resource):
    ps = PolicySet(policies)
    ps.validate()
    return evaluate_policy_set(ps, subject, resource)


# ---- 基础语义 ------------------------------------------------------------

def test_allow_when_only_allow_matches():
    policies = [policy("p1", [rule("a1", "allow", eq("subject.role", "member"))])]
    out = compile_and_eval(policies, {"role": "member"}, {})
    assert out["decision"] == DECISION_ALLOW
    assert out["reason"] == "explicit_allow"
    assert out["minimal_relevant_rules"] == ["a1"]
    assert out["matched_rules"] == ["a1"]
    assert out["conflicting_allow_rules"] == []


def test_default_deny_when_no_rule_matches():
    policies = [policy("p1", [rule("a1", "allow", eq("subject.role", "member"))])]
    out = compile_and_eval(policies, {"role": "guest"}, {})
    assert out["decision"] == DECISION_DENY
    assert out["reason"] == "no_applicable_rule"
    assert out["minimal_relevant_rules"] == []
    assert out["matched_rules"] == []


def test_missing_attribute_unknown_is_treated_as_deny():
    # 条件依赖缺失属性 => UNKNOWN => 规则不生效 => 默认拒绝
    policies = [policy("p1", [
        rule("a1", "allow", {"op": "and", "args": [
            eq("subject.role", "member"),
            in_set("subject.clearances", ["read"]),
        ]}),
    ])]
    # 缺 clearances
    out = compile_and_eval(policies, {"role": "member"}, {})
    assert out["decision"] == DECISION_DENY
    assert out["counts"]["indeterminate"] == 1
    result = {r["rule_id"]: r for r in out["rule_results"]}
    assert result["a1"]["condition_value"] == "unknown"
    assert result["a1"]["fired"] is False


def test_explicit_deny_overrides_allow_same_policy():
    policies = [policy("p1", [
        rule("a1", "allow", eq("subject.dept", "eng")),
        rule("d1", "deny", eq("resource.classification", "secret")),
    ])]
    out = compile_and_eval(policies, {"dept": "eng"}, {"classification": "secret"})
    assert out["decision"] == DECISION_DENY
    assert out["reason"] == "explicit_deny"
    assert out["minimal_relevant_rules"] == ["d1"]
    # 被压制的 allow 出现在冲突集合中
    assert out["conflicting_allow_rules"] == ["a1"]
    assert out["matched_rules"] == ["d1"]
    assert out["counts"]["fired_allow"] == 1
    assert out["counts"]["fired_deny"] == 1


def test_explicit_deny_overrides_allow_across_policies():
    # 多策略：allow 在一个策略里，deny 在另一个策略里，全局 deny-overrides
    policies = [
        policy("p-allow", [rule("a1", "allow", eq("subject.dept", "eng"))]),
        policy("p-deny", [rule("d1", "deny", eq("resource.classification", "secret"))]),
    ]
    out = compile_and_eval(policies, {"dept": "eng"}, {"classification": "secret"})
    assert out["decision"] == DECISION_DENY
    assert out["minimal_relevant_rules"] == ["d1"]
    assert out["conflicting_allow_rules"] == ["a1"]


def test_multiple_allows_minimal_set_is_single_representative():
    policies = [policy("p1", [
        rule("a2", "allow", eq("subject.dept", "eng")),
        rule("a1", "allow", in_set("subject.groups", ["staff"])),
    ])]
    out = compile_and_eval(policies, {"dept": "eng", "groups": ["staff"]}, {})
    assert out["decision"] == DECISION_ALLOW
    assert out["matched_rules"] == ["a1", "a2"]
    # 最小代表性集合：字典序最小的一条，单独即可推出 ALLOW
    assert out["minimal_relevant_rules"] == ["a1"]


def test_multiple_deny_rules_minimal_set_is_single_representative():
    policies = [policy("p1", [
        rule("d2", "deny", eq("resource.classification", "secret")),
        rule("d1", "deny", eq("subject.employment_type", "contractor")),
        rule("a1", "allow", eq("subject.dept", "eng")),
    ])]
    out = compile_and_eval(policies,
                           {"dept": "eng", "employment_type": "contractor"},
                           {"classification": "secret"})
    assert out["decision"] == DECISION_DENY
    assert out["matched_rules"] == ["d1", "d2"]
    assert out["minimal_relevant_rules"] == ["d1"]
    assert out["conflicting_allow_rules"] == ["a1"]


# ---- 顺序无关性：规则与策略全排列 ----------------------------------------

def _rules_fixture():
    return [
        rule("a1", "allow", eq("subject.dept", "eng")),
        rule("a2", "allow", in_set("subject.groups", ["staff"])),
        rule("d1", "deny", eq("resource.classification", "secret")),
        rule("d2", "deny", eq("subject.employment_type", "contractor")),
        rule("a3", "allow", {"op": "and", "args": [
            eq("subject.role", "admin"),
            eq("resource.env", "staging"),
        ]}),
    ]


def test_rule_order_does_not_change_decision_all_permutations():
    """验收项：枚举规则顺序的全部 5! = 120 种排列，结果必须完全一致。"""
    rules = _rules_fixture()
    subject = {"dept": "eng", "groups": ["staff"], "employment_type": "employee", "role": "dev"}
    resource = {"classification": "secret", "env": "prod"}

    seen = None
    for order in itertools.permutations(rules):
        out = compile_and_eval([policy("p1", list(order))], subject, resource)
        snapshot = (
            out["decision"],
            out["reason"],
            out["minimal_relevant_rules"],
            out["matched_rules"],
            out["conflicting_allow_rules"],
        )
        if seen is None:
            seen = snapshot
        assert snapshot == seen, f"规则顺序改变了结果: {order}"

    assert seen[0] == DECISION_DENY
    assert seen[1] == "explicit_deny"
    assert seen[2] == ["d1"]
    assert seen[3] == ["d1"]
    assert seen[4] == ["a1", "a2"]


def test_policy_order_does_not_change_decision_all_permutations():
    """策略层顺序的全部 3! 种排列也不改变结果。"""
    subject = {"dept": "eng", "employment_type": "contractor"}
    resource = {"classification": "secret"}
    policies = [
        policy("pa", [rule("a1", "allow", eq("subject.dept", "eng"))]),
        policy("pd1", [rule("d1", "deny", eq("resource.classification", "secret"))]),
        policy("pd2", [rule("d2", "deny", eq("subject.employment_type", "contractor"))]),
    ]
    seen = None
    for order in itertools.permutations(policies):
        out = compile_and_eval(list(order), subject, resource)
        snapshot = (out["decision"], out["minimal_relevant_rules"],
                    out["matched_rules"], out["conflicting_allow_rules"])
        if seen is None:
            seen = snapshot
        assert snapshot == seen
    assert seen[0] == DECISION_DENY
    assert seen[1] == ["d1"]
    assert seen[2] == ["d1", "d2"]
    assert seen[3] == ["a1"]


def test_random_shuffles_match_reference():
    rng = random.Random(20260924)
    rules = _rules_fixture()
    subject = {"dept": "eng", "groups": ["staff"], "employment_type": "employee"}
    resource = {"classification": "internal"}
    reference = compile_and_eval([policy("p1", rules)], subject, resource)
    for _ in range(50):
        shuffled = rules[:]
        rng.shuffle(shuffled)
        out = compile_and_eval([policy("p1", shuffled)], subject, resource)
        assert out["decision"] == reference["decision"]
        assert out["matched_rules"] == reference["matched_rules"]
        assert out["minimal_relevant_rules"] == reference["minimal_relevant_rules"]


# ---- 组合真值枚举：每条规则独立置真/置假/未知（3^n 输入枚举） -------------

def test_combinatorial_truth_enumeration_three_rules():
    """
    验收项：对 3 条规则（allow A、deny D、allow B）枚举外部世界对每条规则
    条件的 3^3 = 27 种满足情况（真/假/未知），验证 deny-overrides 组合：
      - 任一 deny 为真        => DENY
      - 否则任一 allow 为真   => ALLOW
      - 否则                  => DENY(no_applicable_rule)
    通过直接选择属性值驱动条件，覆盖全部组合。
    """
    policies = [policy("p1", [
        rule("A", "allow", eq("subject.a", "1")),
        rule("D", "deny", eq("subject.d", "1")),
        rule("B", "allow", eq("subject.b", "1")),
    ])]

    # 键值 -> 该规则条件的外部状态；属性缺失表示 UNKNOWN
    states = {
        True: {"v": "1"},      # eq 成立
        False: {"v": "0"},     # eq 可判定为假
        None: {},              # 属性缺失 => 未知
    }

    for sa, sd, sb in itertools.product([True, False, None], repeat=3):
        subject = {}
        if "v" in states[sa]:
            subject["a"] = states[sa]["v"]
        if "v" in states[sd]:
            subject["d"] = states[sd]["v"]
        if "v" in states[sb]:
            subject["b"] = states[sb]["v"]

        out = compile_and_eval(policies, subject, {})
        values = {r["rule_id"]: r["condition_value"] for r in out["rule_results"]}
        name = {True: "true", False: "false", None: "unknown"}
        assert values == {"A": name[sa], "B": name[sb], "D": name[sd]}

        if sd is True:
            assert out["decision"] == DECISION_DENY and out["reason"] == "explicit_deny"
            assert out["minimal_relevant_rules"] == ["D"]
            expected_conflicts = set()
            if sa is True:
                expected_conflicts.add("A")
            if sb is True:
                expected_conflicts.add("B")
            assert set(out["conflicting_allow_rules"]) == expected_conflicts
        elif sa is True or sb is True:
            assert out["decision"] == DECISION_ALLOW
            assert out["reason"] == "explicit_allow"
            fired_allows = [rid for rid, st in (("A", sa), ("B", sb)) if st is True]
            assert out["minimal_relevant_rules"] == [min(fired_allows)]
            assert out["matched_rules"] == sorted(fired_allows)
        else:
            assert out["decision"] == DECISION_DENY
            assert out["reason"] == "no_applicable_rule"
            assert out["minimal_relevant_rules"] == []


# ---- 策略结构校验 --------------------------------------------------------

@pytest.mark.parametrize("policies,msg", [
    ([], "不能为空"),
    ({}, "数组"),
    ([{"id": "p1"}], "rules"),
    ([{"id": "p1", "rules": []}], "非空"),
    ([{"id": "p1", "rules": [{"id": "r1", "effect": "allow"}]}], "condition"),
    ([{"id": "p1", "rules": [{"id": "r1", "effect": "maybe",
                              "condition": {"literal": True}}]}], "effect"),
])
def test_invalid_policy_structure(policies, msg):
    with pytest.raises(PolicyError) as exc:
        ps = PolicySet(policies)
        ps.validate()
    assert any(msg in d for d in exc.value.details)


def test_duplicate_rule_and_policy_ids_rejected():
    # rule id 跨策略全局唯一
    with pytest.raises(PolicyError) as exc:
        PolicySet([
            policy("p1", [rule("r1", "allow", eq("subject.a", "1"))]),
            policy("p2", [rule("r1", "allow", eq("subject.a", "1"))]),
        ]).validate()
    assert any("rule.id 全局重复" in d for d in exc.value.details)

    # policy id 重复
    with pytest.raises(PolicyError) as exc:
        PolicySet([
            policy("p1", [rule("r1", "allow", eq("subject.a", "1"))]),
            policy("p1", [rule("r2", "allow", eq("subject.a", "1"))]),
        ]).validate()
    assert any("policy.id 重复" in d for d in exc.value.details)


def test_minimal_relevant_rule_actually_justifies_decision():
    """最小规则集合的语义校验：移除/翻转代表规则后，结论不再由该规则支撑。"""
    policies = [policy("p1", [
        rule("a1", "allow", eq("subject.dept", "eng")),
        rule("d1", "deny", eq("resource.classification", "secret")),
    ])]
    subject, resource = {"dept": "eng"}, {"classification": "secret"}
    out = compile_and_eval(policies, subject, resource)
    assert out["minimal_relevant_rules"] == ["d1"]

    # 仅保留最小规则集合中的 d1（去掉 allow），拒绝结论依然成立
    only_deny = [policy("p1", [rule("d1", "deny", eq("resource.classification", "secret"))])]
    out2 = compile_and_eval(only_deny, subject, resource)
    assert out2["decision"] == DECISION_DENY

    # 而翻转 d1 的条件后（资源非 secret），allow 决定翻转 => 证明 d1 与决定相关
    out3 = compile_and_eval(policies, subject, {"classification": "public"})
    assert out3["decision"] == DECISION_ALLOW
