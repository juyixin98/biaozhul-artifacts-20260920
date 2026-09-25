"""验收场景四：嵌套结构不得旁路限制。

包括：
* 深层嵌套未知字段逐层受控；
* allow 父对象不放开子字段、deny 父对象整树移除；
* recursive 规则覆盖全部后代；
* 递归规则与精确规则、用途规则之间的优先级。
"""

from mde.engine import apply_policy
from mde.policy import build_policy
from mde.models import Rule


def _p(rules, aliases=()):
    return build_policy("p", 1, rules, aliases=aliases)


def test_deeply_nested_unknown_denied():
    policy = _p([Rule(path="a.b.c.d", action="allow")])
    data = {"a": {"b": {"c": {"d": 1, "e": 2}}, "leak": 3}}
    res = apply_policy(data, policy, "analytics", b"\x09" * 32)
    assert res.output == {"a": {"b": {"c": {"d": 1}}}}


def test_allowed_parent_does_not_leak_children():
    policy = _p([
        Rule(path="user", action="allow"),
        Rule(path="user.profile.name", action="allow"),
    ])
    data = {"user": {
        "profile": {"name": "A", "dob": "1990-01-01"},
        "password_hash": "deadbeef",
    }}
    res = apply_policy(data, policy, "analytics", b"\x0a" * 32)
    assert res.output == {"user": {"profile": {"name": "A"}}}


def test_denied_parent_removes_everything_below():
    policy = _p([
        Rule(path="medical", action="deny"),
        # 即使存在子级 allow，父级 deny 也优先（子树不再遍历）。
        Rule(path="medical.diagnosis", action="allow"),
        Rule(path="keep", action="allow"),
    ])
    data = {"medical": {"diagnosis": "X", "notes": ["n1", "n2"]},
            "keep": 1}
    res = apply_policy(data, policy, "analytics", b"\x0b" * 32)
    assert res.output == {"keep": 1}
    med_recs = [d for d in res.decisions if d["path"].startswith("medical")]
    assert len(med_recs) == 1 and med_recs[0]["decision"] == "deny"


def test_recursive_rule_covers_all_descendants():
    policy = _p([
        Rule(path="public", action="allow", recursive=True),
    ])
    data = {"public": {"a": 1, "b": {"c": [{"d": 2}]}}}
    res = apply_policy(data, policy, "analytics", b"\x0c" * 32)
    assert res.output == data
    assert all(d["decision"] == "allow" for d in res.decisions)


def test_exact_rule_beats_recursive_ancestor():
    # public 递归 allow，public.secret 精确 deny：
    # 在 public.secret 这一路径上精确规则优先于祖先递归规则。
    policy = _p([
        Rule(path="public", action="allow", recursive=True),
        Rule(path="public.secret", action="deny"),
    ])
    data = {"public": {"a": 1, "secret": "s"}}
    res = apply_policy(data, policy, "analytics", b"\x0d" * 32)
    assert res.output == {"public": {"a": 1}}
    rec = [d for d in res.decisions if d["path"] == "public.secret"][0]
    assert rec["decision"] == "deny"
    assert rec["matched_rule_path"] == "public.secret"


def test_recursive_rule_matches_prefix_only():
    # 规则按“路径”匹配，不按任意深度的字段名通配。
    # deny recursive a.b 只影响 a.b 及其后代，不影响 a.x.b。
    policy = _p([
        Rule(path="a.b", action="deny", recursive=True),
        Rule(path="a.x.b", action="allow"),
    ])
    data = {"a": {"b": {"y": 1}, "x": {"b": 2}, "c": 3}}
    res = apply_policy(data, policy, "analytics", b"\x0d" * 32)
    assert res.output == {"a": {"x": {"b": 2}}}


def test_purpose_specific_rule_outranks_general_rule():
    policy = _p([
        Rule(path="salary", action="allow"),  # 通配 allow
        Rule(path="salary", action="deny", purpose="analytics"),  # 分析用途拒绝
    ])
    res = apply_policy({"salary": 100}, policy, "analytics", b"\x0e" * 32)
    assert "salary" not in res.output
    res2 = apply_policy({"salary": 100}, policy, "payroll", b"\x0e" * 32)
    assert res2.output == {"salary": 100}


def test_longer_recursive_rule_outranks_shorter():
    policy = _p([
        Rule(path="a", action="allow", recursive=True),
        Rule(path="a.b", action="deny", recursive=True),
    ])
    data = {"a": {"x": 1, "b": {"y": 2, "z": 3}}}
    res = apply_policy(data, policy, "analytics", b"\x0f" * 32)
    assert res.output == {"a": {"x": 1}}
