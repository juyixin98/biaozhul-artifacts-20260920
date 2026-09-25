"""验收场景三：数组处理与 [] 路径规范化。

要点：
* 规则 contacts[].x 对所有元素一致生效，无法通过具体下标绕过；
* 元素内部仍然逐字段评估；
* 数组下嵌套对象/数组同样受控；
* 标量数组也可被规则直接 allow/deny/generalize。
"""

from mde.engine import apply_policy
from mde.generalizers import g_hash
from mde.policy import build_policy
from mde.models import Rule


def _p(rules, aliases=()):
    return build_policy("p", 1, rules, aliases=aliases)


def test_rule_applies_to_all_elements():
    policy = _p([
        Rule(path="contacts[].name", action="allow"),
        Rule(path="contacts[].phone", action="deny"),
    ])
    data = {"contacts": [
        {"name": "A", "phone": "1"},
        {"name": "B", "phone": "2", "extra": "x"},
        {"name": "C"},
    ]}
    res = apply_policy(data, policy, "analytics", b"\x02" * 32)
    assert res.output == {"contacts": [{"name": "A"},
                                       {"name": "B"},
                                       {"name": "C"}]}
    phone_recs = [d for d in res.decisions
                  if d["path"] == "contacts[].phone"]
    assert len(phone_recs) == 2  # 前两个元素有该字段
    assert all(d["decision"] == "deny" for d in phone_recs)


def test_indexed_access_cannot_bypass_array_rule():
    # 规则只可能以 [] 书写；引擎遍历数组时统一产出 [] 路径，
    # 任何“第 N 个元素”都走同一条规则。
    policy = _p([
        Rule(path="rows[].secret", action="deny"),
        Rule(path="rows[].ok", action="allow"),
        Rule(path="rows[].secret", action="allow",
             purpose="other-purpose"),  # 不同用途的 allow 不影响本用途
    ])
    data = {"rows": [{"secret": i, "ok": i} for i in range(5)]}
    res = apply_policy(data, policy, "analytics", b"\x03" * 32)
    assert all("secret" not in row for row in res.output["rows"])
    assert [row["ok"] for row in res.output["rows"]] == [0, 1, 2, 3, 4]


def test_nested_arrays_path_normalization():
    policy = _p([
        Rule(path="matrix[][].v", action="allow"),
        Rule(path="matrix[][].hidden", action="deny"),
    ])
    data = {"matrix": [
        [{"v": 1, "hidden": "a"}, {"v": 2}],
        [{"v": 3, "hidden": "b"}],
    ]}
    res = apply_policy(data, policy, "analytics", b"\x04" * 32)
    assert res.output == {"matrix": [[{"v": 1}, {"v": 2}], [{"v": 3}]]}


def test_array_level_deny_removes_whole_subtree():
    policy = _p([Rule(path="secret_list", action="deny"),
                 Rule(path="keep", action="allow")])
    data = {"secret_list": [{"a": 1}, {"b": 2}], "keep": 1}
    res = apply_policy(data, policy, "analytics", b"\x05" * 32)
    assert res.output == {"keep": 1}


def test_array_allow_still_evaluates_element_fields():
    policy = _p([
        Rule(path="items", action="allow"),
        Rule(path="items[].id", action="allow"),
        Rule(path="items[].token", action="deny"),
    ])
    data = {"items": [{"id": 1, "token": "t"}, {"id": 2}]}
    res = apply_policy(data, policy, "analytics", b"\x06" * 32)
    assert res.output == {"items": [{"id": 1}, {"id": 2}]}


def test_scalar_array_rule():
    policy = _p([
        Rule(path="tags", action="generalize", generalizer="hash"),
    ])
    salt = b"\x07" * 32
    res = apply_policy({"tags": ["a", "b"]}, policy, "analytics", salt)
    # 数组类型敏感泛化器失败 -> fail closed，整组不导出。
    assert "tags" not in res.output
    rec = [d for d in res.decisions if d["path"] == "tags"][0]
    assert rec["decision"] == "deny"
    assert rec["by"] == "generalizer_failure"


def test_array_records_share_canonical_path_not_index():
    policy = _p([Rule(path="xs[].v", action="allow")])
    res = apply_policy({"xs": [{"v": 1}, {"v": 2}, {"v": 3}]},
                       policy, "analytics", b"\x08" * 32)
    paths = [d["path"] for d in res.decisions if d["decision"] == "allow"]
    assert paths == ["xs[].v", "xs[].v", "xs[].v"]
