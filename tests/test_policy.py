import pytest

from mde.policy import PolicyError, validate_policy


def base_doc():
    return {
        "version": "mde/policy@v1",
        "policy_id": "p",
        "revision": 1,
        "purposes": {
            "u": {
                "default": "deny",
                "allow": ["id", "profile.name"],
                "deny": ["ssn"],
                "generalize": {
                    "email": {"transform": "email_domain", "params": {}},
                },
            }
        },
        "aliases": {"mail": "email"},
    }


def test_valid_policy_compiles():
    cp = validate_policy(base_doc())
    assert len(cp.policy_fingerprint) == 64
    assert cp.purpose("u").default == "deny"


def test_unknown_version_and_keys_rejected():
    doc = base_doc()
    doc["version"] = "other"
    with pytest.raises(PolicyError):
        validate_policy(doc)
    doc = base_doc()
    doc["bogus"] = 1
    with pytest.raises(PolicyError):
        validate_policy(doc)


def test_prefix_overlap_rejected():
    doc = base_doc()
    doc["purposes"]["u"]["allow"].append("profile")  # 与 profile.name 前缀冲突
    with pytest.raises(PolicyError, match="前缀"):
        validate_policy(doc)


def test_duplicate_path_across_actions_rejected():
    doc = base_doc()
    doc["purposes"]["u"]["deny"].append("id")
    with pytest.raises(PolicyError):
        validate_policy(doc)


def test_alias_chain_and_unknown_target_rejected():
    # 链的第一环指向不存在字段 -> 拒绝
    doc = base_doc()
    doc["aliases"] = {"a": "b", "b": "email"}
    with pytest.raises(PolicyError, match="目标必须是"):
        validate_policy(doc)

    # 构造别名链的另一种尝试同样被"目标必须是规则字段"拦住：
    # mail 成为别名源后，它不可能再充当合法的别名目标
    doc = base_doc()
    doc["aliases"] = {"mail": "email", "mail2": "mail"}
    with pytest.raises(PolicyError, match="目标必须是"):
        validate_policy(doc)

    doc = base_doc()
    doc["aliases"] = {"ghost": "not.a.field"}
    with pytest.raises(PolicyError, match="目标必须是"):
        validate_policy(doc)

    doc = base_doc()
    doc["aliases"] = {"id": "email"}  # 别名源遮蔽真实字段
    with pytest.raises(PolicyError, match="冲突"):
        validate_policy(doc)


def test_unknown_transform_and_bad_params_rejected():
    doc = base_doc()
    doc["purposes"]["u"]["generalize"]["age"] = {"transform": "nope"}
    with pytest.raises(PolicyError):
        validate_policy(doc)

    doc = base_doc()
    doc["purposes"]["u"]["generalize"]["age"] = {
        "transform": "bucket_number", "params": {"width": 0, "offset": 0}
    }
    with pytest.raises(PolicyError):
        validate_policy(doc)


def test_purpose_must_exist():
    cp = validate_policy(base_doc())
    with pytest.raises(PolicyError):
        cp.purpose("missing")


def test_fingerprint_stable_and_sensitive_to_content():
    a = validate_policy(base_doc())
    doc2 = base_doc()
    doc2["description"] = "changed"
    b = validate_policy(doc2)
    assert a.policy_fingerprint == validate_policy(base_doc()).policy_fingerprint
    assert a.policy_fingerprint != b.policy_fingerprint
