"""验收场景一：字段别名。

别名映射必须同时对“数据里出现别名、规则写规范名”和“数据里出现规范名、
规则写别名”两种方向生效，且决策记录如实标注路径，无法借别名绕过 deny。
"""

from mde.engine import apply_policy
from mde.policy import build_policy
from mde.models import Rule


def _policy(rules, aliases=()):
    return build_policy("p1", 1, rules, aliases=aliases,
                        description="alias test")


def test_alias_data_key_matches_canonical_rule():
    # 规则写规范名 email，数据里出现别名 mail / email_address 都应命中。
    policy = _policy(
        [Rule(path="email", action="generalize", generalizer="email_mask")],
        aliases=[("email", ["mail", "email_address"])],
    )
    data = {"mail": "alice@example.com"}
    res = apply_policy(data, policy, "analytics", b"\x00" * 32)
    assert res.output == {"mail": "a***@example.com"}
    rec = res.decisions[0]
    assert rec["path"] == "email"
    assert rec["input_path"] == "mail"
    assert rec["resolved_via_alias"] is True
    assert rec["decision"] == "generalize"


def test_alias_second_name_matches():
    policy = _policy(
        [Rule(path="email", action="deny")],
        aliases=[("email", ["mail", "email_address"])],
    )
    data = {"email_address": "bob@example.com", "other": 1}
    res = apply_policy(data, policy, "analytics", b"\x00" * 32)
    assert "email_address" not in res.output
    assert "other" not in res.output  # 未知字段默认拒绝
    paths = {d["path"] for d in res.decisions if d["decision"] == "deny"}
    assert "email" in paths


def test_alias_nested_path_resolution():
    policy = _policy(
        [Rule(path="user.contact_email", action="deny"),
         Rule(path="user.name", action="allow")],
        aliases=[("contact_email", ["cemail", "email_addr"])],
    )
    data = {"user": {"cemail": "x@y.z", "name": "n"}}
    res = apply_policy(data, policy, "analytics", b"\x00" * 32)
    assert res.output == {"user": {"name": "n"}}
    deny = [d for d in res.decisions if d["path"] == "user.contact_email"]
    assert len(deny) == 1 and deny[0]["decision"] == "deny"


def test_alias_cannot_bypass_deny_inside_array():
    # 数组元素里使用别名同样命中 [] 规则。
    policy = _policy(
        [Rule(path="contacts[].contact_email", action="deny"),
         Rule(path="contacts[].keep", action="allow")],
        aliases=[("contact_email", ["cemail"])],
    )
    data = {"contacts": [
        {"cemail": "a@b.c", "keep": 1},
        {"contact_email": "d@e.f", "keep": 2},
    ]}
    res = apply_policy(data, policy, "analytics", b"\x00" * 32)
    assert res.output == {"contacts": [{"keep": 1}, {"keep": 2}]}


def test_alias_decision_records_distinguish_input_and_canonical_path():
    policy = _policy(
        [Rule(path="email", action="allow")],
        aliases=[("email", ["mail"])],
    )
    res = apply_policy({"mail": "a@b.c"}, policy, "analytics", b"\x00" * 32)
    rec = res.decisions[0]
    assert rec["path"] == "email"
    assert rec["input_path"] == "mail"
    assert rec["alias_resolved"] is True
