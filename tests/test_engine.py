import copy

import pytest

from maskcompiler.compiler import compile_ruleset
from maskcompiler.engine import apply_ruleset
from maskcompiler.errors import (
    MissingFieldError,
    RuleConflictError,
    TypeMismatchError,
)


def rule(rid, path, transform, priority, params=None, **extra):
    d = {"id": rid, "path": path, "transform": transform, "priority": priority}
    if params:
        d["params"] = params
    d.update(extra)
    return d


def apply(doc, *rules, bundle=None):
    compiled = compile_ruleset({"version": 1, "rules": list(rules)})
    if bundle is not None:
        compiled.bind_keys(bundle)
    return apply_ruleset(compiled, doc)


# ---------- acceptance: nested arrays + keep-last ----------

def test_nested_arrays_and_keep_last():
    doc = {
        "orders": [
            {"id": 1, "payments": [{"method": "card", "number": "4111111111111234"}]},
            {"id": 2, "payments": [
                {"method": "card", "number": "4222222222222345"},
                {"method": "card", "number": "4333333333333456"},
            ]},
        ]
    }
    result = apply(
        doc,
        rule("pan", "$.orders[*].payments[*].number", "mask", 10, {"keep_last": 4}),
    )
    out = result.output
    assert out["orders"][0]["payments"][0]["number"] == "************1234"
    assert out["orders"][1]["payments"][0]["number"] == "************2345"
    assert out["orders"][1]["payments"][1]["number"] == "************3456"
    assert result.report.matched_rules["pan"] == 3
    # input untouched
    assert doc["orders"][0]["payments"][0]["number"] == "4111111111111234"


def test_recursive_wildcard_over_arrays():
    doc = {"groups": [["p1", "p2"], ["p3"]], "tags": ["p4"]}
    result = apply(doc, rule("all", "$..[*]", "redact", 1))
    # The inner arrays are themselves elements of "groups" and get redacted
    # whole; standalone "tags" loses its elements.
    assert result.output == {"groups": [[None, None], [None]], "tags": [None]}


def test_recursive_wildcard_from_root_covers_root_array():
    doc = [["p1"], "p2", {"k": ["p3"]}]
    result = apply(doc, rule("all", "$..[*]", "redact", 1))
    # The nested ["p3"] is reached as an element of the root array and is
    # redacted whole; the object carrying it also gets its nested array
    # redacted via the descent.
    assert result.output == [[None], None, {"k": None}]


# ---------- acceptance: unicode ----------

def test_unicode_masking_and_hashing(bundle):
    doc = {"users": [{"name": "张三丰"}, {"name": "李四"}]}
    result = apply(
        doc,
        rule("name", "$.users[*].name", "mask", 10, {"keep_first": 1}),
        bundle=bundle,
    )
    assert [u["name"] for u in result.output["users"]] == ["张**", "李*"]

    doc2 = {"field": "敏感值"}
    result2 = apply(doc2, rule("h", "$.field", "hash", 10), bundle=bundle)
    assert isinstance(result2.output["field"], str)
    assert "敏感值" not in result2.output["field"]


# ---------- acceptance: missing fields ----------

def test_missing_field_ignored_by_default():
    doc = {"present": "x"}
    result = apply(doc, rule("r", "$.absent", "redact", 1))
    assert result.output == {"present": "x"}
    assert result.report.matched_rules["r"] == 0


def test_missing_field_error_when_strict():
    with pytest.raises(MissingFieldError) as exc:
        apply(
            {"present": "x"},
            rule("r", "$.absent.deep", "redact", 1, on_missing="error"),
        )
    assert "r" in str(exc.value)
    assert "$.absent.deep" in str(exc.value)


def test_missing_inside_wildcard_with_on_missing_error():
    doc = {"users": [{"phone": "1"}, {}]}
    # one element has the field, so the rule did match overall
    result = apply(
        doc, rule("p", "$.users[*].phone", "redact", 1, on_missing="error")
    )
    assert result.output["users"][0]["phone"] is None
    assert result.output["users"][1] == {}

    with pytest.raises(MissingFieldError):
        apply({"users": [{"x": 1}]},
              rule("p", "$.users[*].phone", "redact", 1, on_missing="error"))


# ---------- acceptance: rule conflicts / priority composition ----------

def test_higher_priority_wins_on_same_field():
    doc = {"user": {"phone": "13812345678"}}
    result = apply(
        doc,
        rule("generic", "$.user.phone", "mask", 10, {"keep_last": 4}),
        rule("strict", "$.user.phone", "redact", 50),
    )
    assert result.output["user"]["phone"] is None


def test_wildcard_vs_specific_rule_composes_by_priority():
    doc = {"user": {"phone": "13812345678", "name": "Alice"}}
    result = apply(
        doc,
        rule("all-fields", "$.user[*]", "redact", 1),
        rule("keep-phone", "$.user.phone", "mask", 100, {"keep_last": 4}),
    )
    assert result.output["user"]["phone"] == "*******5678"
    assert result.output["user"]["name"] is None


def test_equal_priority_dynamic_conflict_raises():
    doc = {"user": {"phone": "13812345678"}}
    with pytest.raises(RuleConflictError) as exc:
        apply(
            doc,
            rule("a", "$.user[*]", "redact", 10),
            rule("b", "$.user.phone", "mask", 10, {"keep_last": 4}),
        )
    msg = str(exc.value)
    assert "a" in msg and "b" in msg
    # error message contains no original value
    assert "13812345678" not in msg


def test_redact_subtree_with_higher_priority_inner_carve_out():
    doc = {"profile": {"name": "Alice", "ssn": "111-22-3333", "city": "Beijing"}}
    result = apply(
        doc,
        rule("nuke", "$.profile", "redact", 1),
        rule("keep-city", "$.profile.city", "mask", 100, {"keep_last": 3}),
    )
    # recurse mode keeps container structure: other keys are redacted in
    # place, only the higher-priority inner field is carved out
    assert result.output["profile"] == {
        "name": None,
        "ssn": None,
        "city": "****ing",
    }


def test_redact_replacement_applies_to_whole_subtree():
    # leaf-level redaction on every member of the card objects
    doc = {"cards": [{"no": "1"}, {"no": "2"}]}
    result = apply(
        doc, rule("r", "$.cards[*].no", "redact", 1, {"replacement": "<gone>"})
    )
    assert result.output == {"cards": [{"no": "<gone>"}, {"no": "<gone>"}]}


def test_redact_whole_array_elements():
    doc = {"cards": [{"no": "1"}, {"no": "2"}]}
    result = apply(
        doc,
        rule("r", "$.cards[*]", "redact", 1, {"container": "replace"}),
    )
    # container=replace: each matched element is replaced wholesale
    assert result.output == {"cards": [None, None]}


def test_redact_replace_mode_replaces_subtree_entirely():
    doc = {"profile": {"name": "Alice", "city": "Beijing"}}
    result = apply(
        doc,
        rule("nuke", "$.profile", "redact", 1,
             {"container": "replace", "replacement": "<redacted>"}),
        rule("keep", "$.profile.city", "mask", 100, {"keep_last": 3}),
    )
    # replace mode wins at $.profile's own location; its subtree goes whole
    assert result.output == {"profile": "<redacted>"}


# ---------- fail-closed typing ----------

def test_scalar_transform_on_container_raises():
    with pytest.raises(TypeMismatchError) as exc:
        apply({"x": {"y": 1}}, rule("r", "$.x", "hash", 1))
    assert "object" in str(exc.value)


def test_scalar_transform_on_number_raises():
    with pytest.raises(TypeMismatchError) as exc:
        apply({"x": 123}, rule("r", "$.x", "mask", 1, {"keep_last": 1}))
    assert "number" in str(exc.value)


def test_null_passes_through():
    result = apply({"x": None}, rule("r", "$.x", "mask", 1, {"keep_last": 1}))
    assert result.output == {"x": None}


def test_bool_is_not_accepted_as_string():
    with pytest.raises(TypeMismatchError):
        apply({"x": True}, rule("r", "$.x", "hash", 1))


# ---------- misc ----------

def test_input_deep_copied_semantics():
    doc = {"a": {"b": ["1", "2"]}}
    original = copy.deepcopy(doc)
    result = apply(doc, rule("r", "$.a.b[*]", "redact", 1))
    assert doc == original
    assert result.output != original


def test_empty_string_is_masked_not_dropped():
    t_doc = {"x": ""}
    result = apply(t_doc, rule("r", "$.x", "mask", 1, {"keep_last": 2}))
    assert result.output["x"] == ""
