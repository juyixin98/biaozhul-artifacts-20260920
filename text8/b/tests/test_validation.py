import pytest

from workflow_engine.expressions import ExpressionError, parse_expression, safe_eval
from workflow_engine.validator import validate_definition


# ----------------------------------------------------------- 表达式安全


@pytest.mark.parametrize(
    "expr",
    [
        "amount > 100",
        'level == "senior"',
        "amount > 100 and vip is True",
        "department in ['finance', 'legal']",
        "x not in (1, 2, 3)",
        "not closed",
        "(a + b) * 2 >= 10",
        "x if flag else y",
        "True",
        "None is None",
    ],
)
def test_allowed_expressions(expr):
    parse_expression(expr)  # 不抛异常即通过


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('id')",
        "open('/etc/passwd')",
        "().__class__.__bases__",
        "[x for x in range(10)]",
        "lambda: 1",
        "f'{amount}'",
        "amount.real",
        "ctx['amount']",
        "print(1)",
        "eval('1+1')",
        "await foo",
        "x := 1",
        "b'abc'",
        "*a, = []",
    ],
)
def test_forbidden_expressions(expr):
    with pytest.raises(ExpressionError):
        parse_expression(expr)


def test_eval_results():
    ctx = {"amount": 500, "vip": True, "department": "finance"}
    assert safe_eval("amount > 100 and vip is True", ctx) is True
    assert safe_eval("department in ['hr']", ctx) is False
    assert safe_eval("amount if vip else 0", ctx) is True


def test_unknown_variable_is_runtime_error():
    with pytest.raises(ExpressionError):
        safe_eval("missing > 1", {})


def test_power_cap():
    with pytest.raises(ExpressionError):
        safe_eval("2 ** 99999", {})


# ----------------------------------------------------------- 模板校验


def _issues(definition):
    return {(i.code, i.node_id) for i in validate_definition(definition)}


def test_valid_definition_passes():
    assert validate_definition(_base_definition()) == []


def _base_definition():
    return {
        "key": "k",
        "name": "n",
        "nodes": [
            {"id": "start", "type": "start", "next": "a"},
            {"id": "a", "type": "approval", "name": "A", "mode": "all", "approvers": ["u1"], "next": "end"},
            {"id": "end", "type": "end", "outcome": "approved"},
        ],
    }


def test_missing_reference_rejected():
    d = _base_definition()
    d["nodes"][0]["next"] = "ghost"
    issues = _issues(d)
    assert any(code == "missing_reference" for code, _ in issues)


def test_unreachable_node_rejected():
    d = _base_definition()
    d["nodes"].append({"id": "orphan", "type": "end", "outcome": "approved"})
    issues = _issues(d)
    assert ("unreachable_node", "orphan") in issues


def test_cycle_rejected():
    d = {
        "key": "k",
        "name": "n",
        "nodes": [
            {"id": "start", "type": "start", "next": "a"},
            {"id": "a", "type": "approval", "name": "A", "mode": "all", "approvers": ["u1"], "next": "b"},
            {"id": "b", "type": "approval", "name": "B", "mode": "all", "approvers": ["u2"], "next": "a", "on_reject": "end1"},
            {"id": "end1", "type": "end", "outcome": "rejected"},
        ],
    }
    issues = _issues(d)
    assert any(code == "cycle" for code, _ in issues)


def test_no_default_condition_rejected():
    d = {
        "key": "k",
        "name": "n",
        "nodes": [
            {"id": "start", "type": "start", "next": "c"},
            {"id": "c", "type": "condition", "branches": [{"when": "x > 1", "next": "end1"}]},
            {"id": "end1", "type": "end", "outcome": "approved"},
        ],
    }
    issues = _issues(d)
    assert ("condition_no_default", "c") in issues


def test_condition_with_literal_true_branch_ok():
    d = {
        "key": "k",
        "name": "n",
        "nodes": [
            {"id": "start", "type": "start", "next": "c"},
            {
                "id": "c",
                "type": "condition",
                "branches": [
                    {"when": "x > 1", "next": "end1"},
                    {"when": "true", "next": "end2"},
                ],
            },
            {"id": "end1", "type": "end", "outcome": "approved"},
            {"id": "end2", "type": "end", "outcome": "rejected"},
        ],
    }
    assert validate_definition(d) == []


def test_edge_to_start_rejected():
    d = _base_definition()
    d["nodes"][1]["next"] = "start"
    issues = _issues(d)
    assert any(code == "edge_to_start" for code, _ in issues)


def test_duplicate_node_and_start_count():
    d = _base_definition()
    d["nodes"].append(d["nodes"][0].copy())
    issues = _issues(d)
    assert any(code == "duplicate_node" for code, _ in issues)

    d2 = _base_definition()
    d2["nodes"] = [n for n in d2["nodes"] if n["type"] != "start"]
    issues2 = _issues(d2)
    assert any(code == "start_count" for code, _ in issues2)


def test_timeout_requires_action_and_non_overlap():
    d = _base_definition()
    d["nodes"][1]["timeout_seconds"] = 60
    issues = _issues(d)
    assert ("timeout_without_action", "a") in issues

    d["nodes"][1]["timeout_action"] = {"type": "escalate", "to": ["u1"]}
    issues = _issues(d)
    assert ("escalation_overlap", "a") in issues

    d["nodes"][1]["timeout_action"] = {"type": "escalate", "to": ["u2"]}
    d["nodes"][1]["on_reject"] = "end"
    assert validate_definition(d) == []
