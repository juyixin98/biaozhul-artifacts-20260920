"""规范化编码与边界输入。"""

import math

import pytest

from mde.canonical import canonical_dumps
from mde.engine import apply_policy
from mde.errors import MdeError
from mde.policy import build_policy
from mde.models import Rule


def test_canonical_key_order_deterministic():
    a = canonical_dumps({"b": 1, "a": 2, "c": {"z": 0, "y": 0}})
    b = canonical_dumps({"c": {"y": 0, "z": 0}, "a": 2, "b": 1})
    assert a == b


def test_canonical_unicode_not_escaped():
    assert canonical_dumps({"名": "值"}) == '{"名":"值"}'.encode("utf-8")


def test_canonical_rejects_nan():
    with pytest.raises(ValueError):
        canonical_dumps(float("nan"))


def test_explicit_null_value_is_a_field_and_decided():
    policy = build_policy("p", 1, [Rule(path="a", action="allow")])
    res = apply_policy({"a": None, "b": None}, policy, "analytics", b"x" * 32)
    assert res.output == {"a": None}
    recs = {d["path"]: d["decision"] for d in res.decisions}
    assert recs == {"a": "allow", "b": "deny"}


def test_root_array_supported():
    policy = build_policy("p", 1, [
        Rule(path="[].id", action="allow"),
        Rule(path="[].secret", action="deny"),
    ])
    res = apply_policy([{"id": 1, "secret": "a"}, {"id": 2}], policy,
                       "analytics", b"x" * 32)
    assert res.output == [{"id": 1}, {"id": 2}]


def test_scalar_root_rejected():
    policy = build_policy("p", 1, [])
    with pytest.raises(MdeError):
        apply_policy("just a string", policy, "analytics", b"x" * 32)


def test_empty_purpose_rejected():
    policy = build_policy("p", 1, [])
    with pytest.raises(MdeError):
        apply_policy({}, policy, "", b"x" * 32)


def test_unsupported_type_rejected_with_strings_intact():
    policy = build_policy("p", 1, [Rule(path="a", action="allow")])
    # JSON 解析不会产生 tuple/set；直接构造以确认引擎明确报错而非静默。
    with pytest.raises(MdeError):
        apply_policy({"a": (1, 2)}, policy, "analytics", b"x" * 32)
