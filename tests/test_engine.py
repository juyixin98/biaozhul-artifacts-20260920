import pytest

from mde.engine import export_records
from mde.paths import join_tokens, split_path
from mde.policy import PolicyError, validate_policy
from mde.transforms import TransformContext


def doc():
    return {
        "version": "mde/policy@v1",
        "policy_id": "cust",
        "revision": 1,
        "purposes": {
            "analytics": {
                "default": "deny",
                "allow": [
                    "id",
                    "age",
                    "prefs.newsletter",
                    "orders[].id",
                    "orders[].amount",
                    "tags[]",
                ],
                "deny": ["ssn", "security", "password_hash"],
                "generalize": {
                    "email": {"transform": "email_domain", "params": {}},
                    "name": {"transform": "pseudonymize", "params": {"scope": "purpose"}},
                    "orders[].placed_at": {
                        "transform": "date_trunc", "params": {"unit": "day"}
                    },
                },
            },
            "support": {
                "default": "deny",
                "allow": ["id", "name", "email", "prefs"],
                "deny": ["ssn", "security", "password_hash", "orders"],
                "generalize": {},
            },
        },
        "aliases": {"e_mail": "email", "orders[].buyer": "id"},
    }


@pytest.fixture()
def compiled():
    return validate_policy(doc())


@pytest.fixture()
def ctx(compiled):
    return TransformContext(b"k" * 32, compiled.policy_fingerprint, "analytics")


def record():
    return {
        "id": "C-1",
        "name": "Zhang",
        "email": "a@example.com",
        "ssn": "secret-ssn",
        "age": 30,
        "password_hash": "ph",
        "prefs": {"newsletter": True, "risk": 9},
        "security": {"totp": "TOTPSECRET", "backup": ["x"]},
        "tags": ["a", "b"],
        "orders": [
            {"id": "O-1", "amount": 100, "placed_at": "2026-01-02T03:04:05",
             "buyer": "C-1", "memo": "internal"},
        ],
        "unknown": {"nested": "x"},
        "e_mail": "evil@attacker.test",
        "user.id": "literal-dotted-key",
    }


def test_analytics_output_is_minimal(compiled, ctx):
    out, decisions = export_records([record()], compiled, "analytics", ctx)
    o = out[0]
    # 允许/泛化的字段
    assert o["id"] == "C-1"
    assert o["age"] == 30
    assert o["email"] == "example.com"
    assert o["name"].startswith("hmac:")
    assert o["prefs"] == {"newsletter": True}          # 兄弟风险字段被剥掉
    assert o["tags"] == ["a", "b"]
    assert o["orders"] == [
        {"id": "O-1", "amount": 100, "placed_at": "2026-01-02", "buyer": "C-1"}
    ]
    # 别名按其指向的规则处理，值保留在原输入路径
    assert o["e_mail"] == "attacker.test"
    # 拒绝 / 未知 / 嵌套旁路尝试 全部不在输出中
    for forbidden in ("ssn", "password_hash", "security", "unknown", "user.id"):
        assert forbidden not in o
    assert "memo" not in o["orders"][0]


def test_every_leaf_has_a_decision(compiled, ctx):
    _, decisions = export_records([record()], compiled, "analytics", ctx)
    leafs = [d for d in decisions if not d["structural"]]
    # 每个输入叶子都有决策，且每条决策路径/动作齐全
    paths = {d["path"] for d in leafs}
    expected = {
        "id", "name", "email", "ssn", "age", "password_hash",
        "prefs.newsletter", "prefs.risk",
        "tags[0]", "tags[1]",
        "orders[0].id", "orders[0].amount", "orders[0].placed_at",
        "orders[0].buyer", "orders[0].memo",
        "unknown.nested", "e_mail", "user%2eid",
    }
    assert expected <= paths, f"缺少决策: {expected - paths}"
    # security 子树被祖先规则整体拒绝：一条结构性决策，整棵丢弃，不逐叶暴露
    struct = {d["shape_path"]: d for d in decisions if d["structural"]}
    assert struct["security"]["decision"] == "deny"
    assert struct["security"]["reason"] == "exact_rule"
    assert not any(p.startswith("security.") for p in paths)
    for d in leafs:
        assert d["decision"] in ("allow", "deny", "generalize")
        assert d["reason"]
        assert d["input_digest"]


def test_alias_decisions_are_auditable(compiled, ctx):
    _, decisions = export_records([record()], compiled, "analytics", ctx)
    by_path = {d["path"]: d for d in decisions if not d["structural"]}
    # e_mail 是 email 的别名 -> 在 analytics 下被泛化，但不能产出真实别名值
    d = by_path["e_mail"]
    assert d["action"] == "generalize"
    assert d["via_alias"] == "e_mail"
    assert d["rule_path"] == "email"
    # orders[].buyer 是 id 的别名 -> 允许
    b = by_path["orders[0].buyer"]
    assert b["decision"] == "allow" and b["via_alias"] == "orders[].buyer"
    assert b["rule_path"] == "id"
    out, _ = export_records([record()], compiled, "analytics", ctx)
    # 别名输入按目标规则处理，泛化/允许值保留在别名路径下
    assert out[0]["e_mail"] == "attacker.test"
    assert out[0]["orders"][0]["buyer"] == "C-1"


def test_unknown_fields_denied_by_default(compiled, ctx):
    _, decisions = export_records([record()], compiled, "analytics", ctx)
    by_path = {d["path"]: d for d in decisions if not d["structural"]}
    assert by_path["unknown.nested"]["decision"] == "deny"
    assert by_path["unknown.nested"]["reason"] == "default_deny"
    # 安全字段是显式拒绝；安全子树整体一条结构性拒绝
    assert by_path["ssn"]["reason"] == "exact_rule"
    struct_paths = {d["shape_path"]: d for d in decisions if d["structural"]}
    assert struct_paths["security"]["decision"] == "deny"


def test_literal_dotted_key_cannot_bypass(compiled, ctx):
    out, decisions = export_records([record()], compiled, "analytics", ctx)
    by_shape = {d["shape_path"]: d for d in decisions if not d["structural"]}
    # 字面键被编码成单段 user%2eid，策略没有该字段 -> 默认拒绝，
    # 绝不可能匹配到（不存在的）嵌套 user.id 或其它 user.* 规则
    assert "user%2eid" in by_shape
    assert by_shape["user%2eid"]["decision"] == "deny"
    assert all(not p.startswith("user.") for p in (d["path"] for d in decisions
             if not d["structural"]))


def test_shape_confusion_does_not_match(compiled, ctx):
    # tags 规则是 tags[]（数组元素）。把 tags 换成对象/标量 -> 规则不命中 -> 拒绝
    rec = record()
    rec["tags"] = {"0": "x", "1": "y"}
    out, decisions = export_records([rec], compiled, "analytics", ctx)
    assert "tags" not in out[0]
    assert any(d["path"] == "tags.0" and d["decision"] == "deny" for d in decisions)
    assert any(d["path"] == "tags.1" and d["decision"] == "deny" for d in decisions)

    # 把 orders（对象数组）换成标量数组：orders[].id 等不再命中
    rec2 = record()
    rec2["orders"] = [999]
    out2, dec2 = export_records([rec2], compiled, "analytics", ctx)
    assert "orders" not in out2[0]  # 元素全部被拒，空壳整体移除
    assert any(d["path"] == "orders[0]" and d["decision"] == "deny" for d in dec2)


def test_empty_arrays_policy_known_vs_unknown(compiled, ctx):
    rec = {"id": "C", "tags": [], "orders": [], "mystery": []}
    out, decisions = export_records([rec], compiled, "analytics", ctx)
    assert out[0]["tags"] == []        # tags[] 有规则 -> 保留空数组
    assert out[0]["orders"] == []      # orders[].* 有规则 -> 保留
    assert "mystery" not in out[0]     # 完全未知 -> 连存在性都不泄露
    struct = {d["path"]: d for d in decisions if d["structural"]}
    assert struct["mystery"]["reason"] == "empty_unknown_container_denied"


def test_purpose_isolation(compiled, ctx):
    rec = record()
    out, _ = export_records([rec], compiled, "support",
                            TransformContext(b"k" * 32, compiled.policy_fingerprint, "support"))
    o = out[0]
    assert o["email"] == "a@example.com"     # support 明文允许
    assert o["name"] == "Zhang"
    assert o["prefs"] == {"newsletter": True, "risk": 9}  # 子树整体允许
    assert "orders" not in o                  # 显式拒绝整棵订单
    assert "ssn" not in o


def test_pseudonym_is_purpose_bound_and_deterministic(compiled):
    c1 = TransformContext(b"k" * 32, compiled.policy_fingerprint, "analytics")
    c2 = TransformContext(b"k" * 32, compiled.policy_fingerprint, "analytics")
    (v1,), _ = export_records([{"name": "Zhang"}], compiled, "analytics", c1)
    (v2,), _ = export_records([{"name": "Zhang"}], compiled, "analytics", c2)
    assert v1["name"] == v2["name"]            # 同目的+同策略：稳定可复算
    # 目的绑定：analytics 与 support 目的派生出不同密钥
    assert c1.purpose_key("analytics") != c1.purpose_key("support")
    # 策略绑定：同目的、不同策略指纹派生不同密钥（跨数据集不可直接关联）
    c_other_fp = TransformContext(b"k" * 32, "other-fingerprint", "analytics")
    assert c1.purpose_key("analytics") != c_other_fp.purpose_key("analytics")
    # 根密钥不同也不同
    c_other_secret = TransformContext(b"j" * 32, compiled.policy_fingerprint, "analytics")
    assert c1.purpose_key("analytics") != c_other_secret.purpose_key("analytics")


def test_transform_failure_drops_value_with_reason(compiled, ctx):
    rec = {"id": "C", "email": "not-an-email"}
    out, decisions = export_records([rec], compiled, "analytics", ctx)
    assert "email" not in out[0]
    d = next(d for d in decisions if d["path"] == "email")
    assert d["decision"] == "deny" and d["reason"] == "transform_failed"
    assert d["warning"]


def test_deeply_nested_unknown_subtree_fully_denied(compiled, ctx):
    rec = {"id": "C", "x": {"y": {"z": ["p", {"q": "r"}]}}}
    out, decisions = export_records([rec], compiled, "analytics", ctx)
    assert out[0] == {"id": "C"}
    denied_paths = {d["path"] for d in decisions if d["decision"] == "deny"}
    assert "x.y.z[0]" in denied_paths and "x.y.z[1].q" in denied_paths


def test_records_must_be_object_array(compiled, ctx):
    with pytest.raises(ValueError):
        export_records(["scalar"], compiled, "analytics", ctx)
    with pytest.raises(PolicyError):
        export_records([{"id": 1}], compiled, "nope", ctx)
