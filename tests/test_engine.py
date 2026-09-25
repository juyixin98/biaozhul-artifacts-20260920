import copy
import json

import pytest

from dms.crypto import CryptoProvider
from dms.engine import apply_rules
from dms.errors import (
    MissingFieldError,
    RuleConflictError,
    TransformError,
)
from dms.rules import compile_rules


def rules(*r):
    return compile_rules({"version": 1, "rules": list(r)})


def R(**kw):
    base = {"id": kw.pop("id", "r"), "action": kw.pop("action", "mask"),
            "path": kw.pop("path", "$.x")}
    base.update(kw)
    return base


# ---------- 基础动作 ----------

def test_mask_keeps_last_four_characters():
    out = apply_rules(rules(R(path="$.phone", options={"keep_last": 4})),
                      {"phone": "13812345678"})
    assert out.document == {"phone": "*******5678"}
    assert out.stats["matched_fields"] == 1


def test_mask_keep_last_zero():
    out = apply_rules(rules(R(path="$.x", options={"keep_last": 0, "mask_char": "#"})),
                      {"x": "secret"})
    assert out.document == {"x": "######"}


def test_mask_unicode_counts_code_points():
    doc = {"name": "张三丰日文テスト"}  # 8 个码点
    out = apply_rules(rules(R(path="$.name", options={"keep_last": 3})), doc)
    assert out.document == {"name": "*" * 5 + "テスト"}


def test_mask_emoji():
    doc = {"x": "ab😀😀"}
    out = apply_rules(rules(R(path="$.x", options={"keep_last": 1})), doc)
    assert out.document == {"x": "*" * 3 + "😀"}


def test_redact_replaces_with_constant():
    out = apply_rules(rules(R(id="r", action="redact", path="$.note",
                              options={"replacement": "【隐】"})),
                      {"note": "秘密"})
    assert out.document == {"note": "【隐】"}


def test_drop_removes_key_and_array_element():
    doc = {"keep": 1, "drop_me": 2, "items": [{"drop_me": 3, "keep": 4}, 5]}
    out = apply_rules(rules(R(id="d", action="drop", path="$..drop_me")), doc)
    assert out.document == {"keep": 1, "items": [{"keep": 4}, 5]}


def test_input_document_not_mutated():
    doc = {"phone": "13812345678"}
    snapshot = copy.deepcopy(doc)
    apply_rules(rules(R(path="$.phone")), doc)
    assert doc == snapshot


# ---------- 嵌套数组 ----------

def test_nested_arrays_masked():
    doc = {"users": [
        {"phones": ["13812345678", "13900001111"]},
        {"phones": ["13700002222"]},
    ]}
    out = apply_rules(rules(R(path="$.users[*].phones[*]", options={"keep_last": 4})),
                      doc)
    assert out.document == {"users": [
        {"phones": ["*******5678", "*******1111"]},
        {"phones": ["*******2222"]},
    ]}


def test_wildcard_applies_inside_nested_object_arrays():
    doc = {"orders": [{"items": [{"sku": "A1"}, {"sku": "A2"}]},
                      {"items": [{"sku": "B1"}]}]}
    out = apply_rules(rules(R(action="hash", path="$.orders[*].items[*].sku")),
                      doc, crypto=CryptoProvider.generate())
    skus = [out.document["orders"][0]["items"][0]["sku"],
            out.document["orders"][1]["items"][0]["sku"]]
    assert all(len(s) == 64 and s != "A1" and s != "B1" for s in skus)
    # 确定性：同值同哈希
    out2 = apply_rules(rules(R(action="hash", path="$.orders[*].items[*].sku")),
                       doc, crypto=CryptoProvider.generate())
    assert out2.document == out.document


def test_recursive_descent_into_arrays():
    doc = {"note": "x", "a": [{"note": "y"}, {"b": {"note": "z"}}]}
    out = apply_rules(rules(R(action="redact", path="$..note")), doc)
    assert out.document == {"note": "***REDACTED***",
                            "a": [{"note": "***REDACTED***"},
                                  {"b": {"note": "***REDACTED***"}}]}


# ---------- 缺失字段 ----------

def test_missing_field_ignored_by_default():
    out = apply_rules(rules(R(path="$.a.b.c")), {"a": {"b": {}}})
    assert out.document == {"a": {"b": {}}}
    assert out.stats["rules"][0]["matched"] == 0


def test_missing_field_required_raises():
    with pytest.raises(MissingFieldError) as ei:
        apply_rules(rules(R(path="$.a.b.c", require_match=True)), {"a": {"b": {}}})
    assert ei.value.details["rule_id"] == "r"
    # 消息不得包含任何字段值
    assert "secret" not in str(ei.value)


def test_missing_field_inside_wildcard_raises_only_when_zero_matches():
    doc = {"users": [{"phone": "1"}, {"phone": "2"}]}
    out = apply_rules(rules(R(path="$.users[*].phone", require_match=True,
                              options={"keep_last": 0})), doc)
    assert out.document == {"users": [{"phone": "*"}, {"phone": "*"}]}
    with pytest.raises(MissingFieldError):
        apply_rules(rules(R(path="$.users[*].email", require_match=True)), doc)


# ---------- 规则冲突与优先级合成 ----------

def test_equal_priority_overlapping_wildcards_conflicts():
    doc = {"users": [{"name": "张三", "secret": "x"}]}
    with pytest.raises(RuleConflictError):
        apply_rules(rules(
            R(id="blanket", action="redact", path="$.users[*]"),
            R(id="specific", action="mask", path="$.users[*].secret",
              options={"keep_last": 0}),
        ), doc)


def test_explicit_priority_resolves_conflict_descendant_wins():
    doc = {"users": [{"name": "张三", "secret": "token-abc"}]}
    out = apply_rules(rules(
        R(id="blanket", action="redact", path="$.users[*]", priority=5),
        R(id="specific", action="mask", path="$.users[*].secret", priority=20,
          options={"keep_last": 3}),
    ), doc)
    # name 被祖先规则 redact；secret 被高优先级后代规则 mask
    assert out.document == {"users": [
        {"name": "***REDACTED***", "secret": "******abc"}]}


def test_explicit_priority_ancestor_wins_entire_subtree():
    doc = {"users": [{"name": "张三", "secret": "token-abc"}]}
    out = apply_rules(rules(
        R(id="blanket", action="redact", path="$.users[*]", priority=20),
        R(id="specific", action="mask", path="$.users[*].secret", priority=5,
          options={"keep_last": 3}),
    ), doc)
    # 祖先优先级更高：specific 不作用，整个子树的标量叶节点被 blanket 替换，
    # 结构保留
    assert out.document == {"users": [
        {"name": "***REDACTED***", "secret": "***REDACTED***"}]}


def test_same_field_higher_priority_rule_wins():
    doc = {"x": "secretvalue"}  # 11 个字符
    out = apply_rules(rules(
        R(id="low", action="redact", path="$.x", priority=1),
        R(id="high", action="mask", path="$.x", priority=10,
          options={"keep_last": 2}),
    ), doc)
    assert out.document == {"x": "*" * 9 + "ue"}


def test_equal_priority_same_field_different_rules_conflicts_at_compile():
    from dms.errors import RuleCompileError
    with pytest.raises(RuleCompileError):
        apply_rules(rules(
            R(id="a", action="mask", path="$.x"),
            R(id="b", action="redact", path="$.x"),
        ), {"x": "v"})


def test_equal_priority_different_fields_no_conflict():
    out = apply_rules(rules(
        R(id="a", action="mask", path="$.a", options={"keep_last": 0}),
        R(id="b", action="mask", path="$.b", options={"keep_last": 0}),
    ), {"a": "12", "b": "34"})
    assert out.document == {"a": "**", "b": "**"}


# ---------- 密码学动作 ----------

def test_encrypt_decrypt_roundtrip(tmp_path):
    cp = CryptoProvider.generate()
    key_file = cp.save(tmp_path / "key.json")
    cp2 = CryptoProvider.load(key_file)
    doc = {"token": "abc-123"}
    out = apply_rules(rules(R(action="encrypt", path="$.token")), doc, crypto=cp)
    assert out.document["token"] != "abc-123"
    back = apply_rules(
        compile_rules({"version": 1, "rules": [
            R(id="dec", action="decrypt", path="$.token")]}),
        out.document, crypto=cp2)
    assert back.document == doc


def test_hash_is_deterministic_sha256_hex():
    cp = CryptoProvider.generate()
    out = apply_rules(rules(R(action="hash", path="$.x")), {"x": "hello"}, crypto=cp)
    import hashlib
    assert out.document["x"] == hashlib.sha256(b"hello").hexdigest()


def test_encrypt_without_provider_raises():
    with pytest.raises(TransformError):
        apply_rules(rules(R(action="encrypt", path="$.x")), {"x": "v"})


def test_decrypt_invalid_ciphertext_does_not_leak_input():
    cp = CryptoProvider.generate()
    secret_value = "gAAAAABh_fake_token_value_that_is_not_valid"
    with pytest.raises(TransformError) as ei:
        apply_rules(rules(R(action="decrypt", path="$.x")),
                    {"x": secret_value}, crypto=cp)
    assert secret_value not in str(ei.value)
    assert "fake_token" not in str(ei.value)


def test_mask_on_non_string_raises_without_value_in_message():
    with pytest.raises(TransformError) as ei:
        apply_rules(rules(R(path="$.x", options={"keep_last": 1})), {"x": 123456})
    assert "123456" not in str(ei.value)


def test_hash_supports_numbers():
    cp = CryptoProvider.generate()
    out = apply_rules(rules(R(action="hash", path="$.x")), {"x": 42}, crypto=cp)
    import hashlib
    assert out.document["x"] == hashlib.sha256(b"42").hexdigest()


# ---------- 统计与 JSON 合法性 ----------

def test_stats_shape():
    doc = {"a": {"b": [{"v": "1234"}]}}
    out = apply_rules(rules(
        R(id="m", action="mask", path="$..v", options={"keep_last": 1}),
        R(id="absent", action="redact", path="$.nope")), doc)
    by_id = {s["id"]: s for s in out.stats["rules"]}
    assert by_id["m"]["matched"] == 1 and by_id["m"]["applied"] == 1
    assert by_id["absent"]["matched"] == 0
    # 输出可被严格 JSON 序列化（无 NaN 等）
    json.dumps(out.document, allow_nan=False)


def test_nan_document_rejected():
    with pytest.raises(TransformError):
        apply_rules(rules(R(action="redact", path="$.x")), {"x": float("nan")})
