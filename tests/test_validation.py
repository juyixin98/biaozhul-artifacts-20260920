import pytest

from app.expressions import ConditionSyntaxError, evaluate
from app.validation import validate_definition


def test_expression_allows_comparison_and_boolean():
    ctx = {"amount": 5000, "region": "cn"}
    assert evaluate("amount >= 1000 and region == 'cn'", ctx) is True
    assert evaluate("amount < 1000 or region == 'us'", ctx) is False
    assert evaluate("not (amount == 5000)", ctx) is False


def test_expression_missing_variable_is_null_like():
    assert evaluate("missing == None", {}) is True
    assert evaluate("missing >= 1", {}) is False  # NULL semantics
    assert evaluate("missing == 1", {}) is False


def test_expression_blocks_arbitrary_code():
    malicious = [
        "__import__('os').system('echo pwned')",
        "().__class__.__bases__[0].__subclasses__()",
        "open('/etc/passwd').read()",
        "x if __import__ else y",
        "lambda: 1",
        "1 .__class__",
    ]
    for expr in malicious:
        with pytest.raises(ConditionSyntaxError):
            evaluate(expr, {"x": 1})


def _linear_template():
    return {
        "nodes": [
            {"id": "s", "type": "start", "next_node": "a"},
            {"id": "a", "type": "approval", "strategy": "all",
             "approvers": ["u1"], "next_node": "e"},
            {"id": "e", "type": "end"},
        ]
    }


def test_valid_definition_passes():
    assert validate_definition(_linear_template()) == []


def test_missing_reference_rejected():
    definition = _linear_template()
    definition["nodes"][1]["next_node"] = "ghost"
    issues = validate_definition(definition)
    assert any(i.code == "missing_reference" for i in issues)


def test_unreachable_node_rejected():
    definition = _linear_template()
    definition["nodes"].append(
        {"id": "orphan", "type": "approval", "strategy": "any",
         "approvers": ["x"], "next_node": "e"}
    )
    issues = validate_definition(definition)
    assert any(i.code == "unreachable" for i in issues)


def test_cycle_rejected():
    definition = {
        "nodes": [
            {"id": "s", "type": "start", "next_node": "c"},
            {"id": "c", "type": "condition",
             "branches": [{"expression": "x == 1", "next_node": "a"}],
             "default": "a"},
            {"id": "a", "type": "approval", "strategy": "any",
             "approvers": ["u"], "next_node": "c"},
        ]
    }
    issues = validate_definition(definition)
    assert any(i.code == "cycle" for i in issues)


def test_start_and_end_counts_validated():
    no_end = {
        "nodes": [
            {"id": "s", "type": "start", "next_node": "a"},
            {"id": "a", "type": "approval", "strategy": "any",
             "approvers": ["u"], "next_node": "s"},
        ]
    }
    issues = validate_definition(no_end)
    assert any(i.code in ("end_count", "cycle") for i in issues)
    assert any(i.code == "end_count" for i in issues)


def test_bad_condition_expression_rejected():
    definition = _linear_template()
    definition["nodes"][0] = {"id": "s", "type": "start", "next_node": "c"}
    definition["nodes"].insert(
        1,
        {"id": "c", "type": "condition",
         "branches": [{"expression": "amount >= )", "next_node": "a"}],
         "default": "e"},
    )
    issues = validate_definition(definition)
    assert any(i.code == "bad_expression" for i in issues)


def test_escalation_target_cannot_be_approver():
    definition = _linear_template()
    definition["nodes"][1]["timeout_seconds"] = 10
    definition["nodes"][1]["escalation_target"] = "u1"
    issues = validate_definition(definition)
    assert any(i.code == "node_schema" for i in issues)


@pytest.mark.asyncio
async def test_api_rejects_invalid_template(client):
    bad = _linear_template()
    bad["nodes"][1]["next_node"] = "ghost"
    resp = await client.post(
        "/templates/bad-code/versions",
        json={"name": "bad", "definition": bad},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_template"
