import pytest

from dms.errors import RuleCompileError, UnknownRuleError
from dms.rules import compile_rules


def base_rule(**over):
    r = {"id": "r1", "action": "mask", "path": "$.a"}
    r.update(over)
    return r


def spec(*rules):
    return {"version": 1, "rules": list(rules)}


def test_minimal_rule_compiles_with_defaults():
    c = compile_rules(spec(base_rule()))
    assert c.rules[0].options == {"keep_last": 4, "mask_char": "*"}
    assert c.rules[0].priority == 0
    assert c.rules[0].require_match is False


def test_unknown_action_rejected_by_default():
    with pytest.raises(UnknownRuleError) as ei:
        compile_rules(spec(base_rule(action="frobnicate")))
    assert "frobnicate" in str(ei.value)


def test_unknown_top_level_field_rejected():
    s = spec(base_rule())
    s["extra"] = 1
    with pytest.raises(RuleCompileError) as ei:
        compile_rules(s)
    assert "extra" in ei.value.details["unknown"]


def test_unknown_rule_field_rejected():
    with pytest.raises(RuleCompileError) as ei:
        compile_rules(spec(base_rule(prio=10)))
    assert "prio" in ei.value.details["unknown"]


def test_unknown_option_rejected():
    with pytest.raises(RuleCompileError) as ei:
        compile_rules(spec(base_rule(options={"keep_last": 2, "keep_first": 1})))
    assert "keep_first" in ei.value.details["unknown"]


def test_bad_option_type_rejected():
    with pytest.raises(RuleCompileError):
        compile_rules(spec(base_rule(options={"keep_last": "4"})))
    with pytest.raises(RuleCompileError):
        compile_rules(spec(base_rule(options={"keep_last": True})))
    with pytest.raises(RuleCompileError):
        compile_rules(spec(base_rule(options={"mask_char": "**"})))


@pytest.mark.parametrize("action,opts", [
    ("redact", {"replacement": "X"}),
    ("drop", {}),
    ("hash", {}),
    ("encrypt", {}),
    ("decrypt", {}),
])
def test_all_known_actions_compile(action, opts):
    c = compile_rules(spec({"id": action, "action": action, "path": "$.x",
                            "options": opts}))
    assert c.rules[0].action == action


def test_priority_must_be_integer():
    with pytest.raises(RuleCompileError):
        compile_rules(spec(base_rule(priority="high")))


def test_duplicate_id_rejected():
    with pytest.raises(RuleCompileError):
        compile_rules(spec(base_rule(id="dup"), base_rule(id="dup", path="$.b")))


def test_same_path_same_priority_different_action_conflict():
    with pytest.raises(RuleCompileError) as ei:
        compile_rules(spec(
            {"id": "a", "action": "mask", "path": "$.x"},
            {"id": "b", "action": "hash", "path": "x"},
        ))
    assert ei.value.details["rule_ids"] == ["a", "b"]


def test_same_path_different_priority_allowed():
    c = compile_rules(spec(
        {"id": "a", "action": "mask", "path": "$.x", "priority": 1},
        {"id": "b", "action": "hash", "path": "x", "priority": 10},
    ))
    assert len(c.rules) == 2


def test_empty_ruleset_rejected():
    with pytest.raises(RuleCompileError):
        compile_rules({"version": 1, "rules": []})


def test_unsupported_version_rejected():
    with pytest.raises(RuleCompileError):
        compile_rules({"version": 2, "rules": [base_rule()]})
