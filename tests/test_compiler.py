import pytest

from maskcompiler.compiler import compile_ruleset
from maskcompiler.errors import (
    RuleConflictError,
    RuleParameterError,
    RuleSyntaxError,
    UnknownTransformError,
)


def rule(rid, path, transform, priority, **extra):
    d = {"id": rid, "path": path, "transform": transform, "priority": priority}
    d.update(extra)
    return d


def ruleset(*rules, **extra):
    doc = {"version": 1, "rules": list(rules)}
    doc.update(extra)
    return doc


def test_minimal_ruleset_compiles():
    compiled = compile_ruleset(ruleset(rule("r1", "$.x", "redact", 0)))
    assert [e.rule_id for e in compiled.entries] == ["r1"]


def test_rules_sorted_by_priority_then_specificity():
    compiled = compile_ruleset(
        ruleset(
            rule("low", "$.a", "redact", 1),
            rule("high", "$.a.b", "redact", 10),
        )
    )
    assert [e.rule_id for e in compiled.entries] == ["high", "low"]


def test_missing_version_rejected():
    with pytest.raises(RuleSyntaxError):
        compile_ruleset({"rules": []})


def test_non_object_ruleset_rejected():
    with pytest.raises(RuleSyntaxError):
        compile_ruleset([1, 2, 3])


def test_unknown_top_level_key_rejected():
    with pytest.raises(RuleSyntaxError):
        compile_ruleset(ruleset(rule("r", "$.x", "redact", 0), defaults={}))


def test_unknown_rule_key_rejected():
    with pytest.raises(RuleSyntaxError):
        compile_ruleset(ruleset(rule("r", "$.x", "redact", 0, when="always")))


def test_duplicate_rule_id_rejected():
    with pytest.raises(RuleSyntaxError):
        compile_ruleset(
            ruleset(rule("r", "$.x", "redact", 0), rule("r", "$.y", "redact", 1))
        )


def test_priority_must_be_non_negative_int():
    with pytest.raises(RuleParameterError):
        compile_ruleset(ruleset(rule("r", "$.x", "redact", -1)))
    with pytest.raises(RuleParameterError):
        compile_ruleset(ruleset(rule("r", "$.x", "redact", "high")))


def test_on_missing_whitelist():
    with pytest.raises(RuleParameterError):
        compile_ruleset(ruleset(rule("r", "$.x", "redact", 0, on_missing="warn")))


def test_unknown_transform_and_params_fail_closed():
    with pytest.raises(UnknownTransformError):
        compile_ruleset(ruleset(rule("r", "$.x", "scramble", 0)))
    with pytest.raises(RuleParameterError):
        compile_ruleset(
            ruleset(rule("r", "$.x", "mask", 0, params={"keep_last": 1, "left": 2}))
        )


def test_bad_path_fails_compilation():
    with pytest.raises(RuleSyntaxError):
        compile_ruleset(ruleset(rule("r", "x.y", "redact", 0)))


def test_equal_priority_same_path_is_static_conflict():
    with pytest.raises(RuleConflictError):
        compile_ruleset(
            ruleset(
                rule("r1", "$.user.phone", "mask", 5, params={"keep_last": 4}),
                rule("r2", "$.user.phone", "hash", 5),
            )
        )


def test_different_priority_same_path_compiles():
    compiled = compile_ruleset(
        ruleset(
            rule("r1", "$.user.phone", "mask", 5, params={"keep_last": 4}),
            rule("r2", "$.user.phone", "hash", 10),
        )
    )
    assert compiled.entries[0].rule_id == "r2"
