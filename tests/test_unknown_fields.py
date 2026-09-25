"""验收场景二：未知字段默认拒绝（fail closed）。"""

from mde.engine import apply_policy
from mde.policy import build_policy
from mde.models import Rule


def _policy(rules, default_action="deny"):
    return build_policy("p1", 1, rules, default_action=default_action)


def test_unknown_scalar_field_denied_by_default():
    policy = _policy([Rule(path="known", action="allow")])
    res = apply_policy({"known": 1, "injected": "secret"}, policy,
                       "analytics", b"\x01" * 32)
    assert res.output == {"known": 1}
    deny = [d for d in res.decisions if d["path"] == "injected"][0]
    assert deny["decision"] == "deny"
    assert deny["by"] == "default"
    assert deny["default_denied"] is True
    assert res.stats["denied_by_default"] >= 1


def test_unknown_nested_field_denied_even_when_parent_allowed():
    # 允许整个容器并不等于允许其内部未知字段裸出。
    policy = _policy([
        Rule(path="profile", action="allow"),
        Rule(path="profile.name", action="allow"),
    ])
    data = {"profile": {"name": "A", "secret_note": "x",
                        "deep": {"ssn": "1"}}}
    res = apply_policy(data, policy, "analytics", b"\x01" * 32)
    assert res.output == {"profile": {"name": "A"}}


def test_newly_added_field_in_array_denied():
    policy = _policy([
        Rule(path="items[].id", action="allow"),
        Rule(path="items[].label", action="allow"),
    ])
    data = {"items": [
        {"id": 1, "label": "a", "internal_flag": True},
        {"id": 2, "label": "b"},
    ]}
    res = apply_policy(data, policy, "analytics", b"\x01" * 32)
    assert res.output == {"items": [{"id": 1, "label": "a"},
                                    {"id": 2, "label": "b"}]}


def test_default_deny_removes_field_but_surviving_siblings_kept():
    policy = _policy([Rule(path="a", action="allow")])
    res = apply_policy({"a": 1, "b": {"x": 1}}, policy, "analytics",
                       b"\x01" * 32)
    assert res.output == {"a": 1}  # b 子树全是未知字段，整体不出现


def test_explicit_deny_overrides_default_allow():
    # 即便策略默认 allow，显式 deny 仍然生效；同用途精确规则优先。
    policy = _policy([Rule(path="ssn", action="deny")],
                     default_action="allow")
    res = apply_policy({"ssn": "1", "name": "n"}, policy, "analytics",
                       b"\x01" * 32)
    assert res.output == {"name": "n"}


def test_empty_containers_are_dropped_but_recorded():
    policy = _policy([Rule(path="keep", action="allow")])
    data = {"keep": 1, "empty_obj": {"gone": 1}, "empty_arr": [{"gone": 2}]}
    res = apply_policy(data, policy, "analytics", b"\x01" * 32)
    assert res.output == {"keep": 1}
    # 容器 passthrough 与子字段 deny 都留痕。
    by_path = {d["path"]: d["decision"] for d in res.decisions}
    assert by_path["empty_obj"] == "passthrough"
    assert by_path["empty_obj.gone"] == "deny"
    assert by_path["empty_arr[]"] == "passthrough"
    assert by_path["empty_arr[].gone"] == "deny"
