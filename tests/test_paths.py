"""路径解析与规范名处理。"""

import pytest

from mde.errors import PolicyValidationError
from mde.models import ARRAY_SEGMENT, Policy, Rule, parse_path, render_path


def test_parse_render_roundtrip():
    cases = {
        "user": ["user"],
        "user.addr.city": ["user", "addr", "city"],
        "contacts[].email": ["contacts", "[]", "email"],
        "a[].b[].c": ["a", "[]", "b", "[]", "c"],
        "x[]": ["x", "[]"],
    }
    for path, segs in cases.items():
        assert parse_path(path) == segs
        assert render_path(segs) == path


def test_parse_rejects_indexed_brackets():
    # 具体下标不允许出现在规则路径中——数组统一用 []。
    with pytest.raises(PolicyValidationError):
        parse_path("contacts[0].email")


def test_parse_rejects_empty_and_malformed():
    for bad in ("", ".a", "a..b", "a[", "a]", "a[.b", "a[b]"):
        with pytest.raises(PolicyValidationError):
            parse_path(bad)


def test_rule_rejects_unknown_action_and_generalizer():
    with pytest.raises(PolicyValidationError):
        Rule(path="a", action="maybe")
    with pytest.raises(PolicyValidationError):
        Rule(path="a", action="generalize")
    with pytest.raises(PolicyValidationError):
        Rule(path="a", action="generalize", generalizer="nope")
    # generalizer 只允许与 generalize 搭配。
    with pytest.raises(PolicyValidationError):
        Rule(path="a", action="allow", generalizer="mask")


def test_alias_conflict_rejected():
    with pytest.raises(PolicyValidationError):
        Policy(policy_id="p", version=1,
               rules=(),
               aliases=(("email", ("mail",)), ("contact_email", ("mail",))))


def test_policy_fingerprint_is_content_bound():
    r1 = (Rule(path="a", action="allow"),)
    p1 = Policy(policy_id="p", version=1, rules=r1)
    p2 = Policy(policy_id="p", version=2, rules=r1)
    p3 = Policy(policy_id="p", version=1,
                rules=(Rule(path="a", action="deny"),))
    assert p1.fingerprint != p2.fingerprint  # 版本不同
    assert p1.fingerprint != p3.fingerprint  # 规则不同
    # 同样内容 -> 同指纹（created_at 固定）。
    p1b = Policy(policy_id="p", version=1, rules=r1,
                 created_at=p1.created_at)
    assert p1b.fingerprint == p1.fingerprint
