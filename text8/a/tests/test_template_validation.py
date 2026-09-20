"""模板发布前校验：缺失引用、不可达节点、环路、受限表达式、结构错误。"""
from __future__ import annotations

import pytest

from app.dsl import validate_definition
from app.errors import ValidationError


def _base():
    return {
        "start_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "next": "a"},
            {"id": "a", "type": "approval", "mode": "all", "assignees": ["x"], "next": "end"},
            {"id": "end", "type": "end"},
        ],
    }


def test_valid_definition_passes():
    validate_definition(_base())


def test_missing_reference_rejected():
    d = _base()
    d["nodes"][1]["next"] = "ghost"
    with pytest.raises(ValidationError) as exc:
        validate_definition(d)
    assert any("不存在" in m for m in exc.value.details)


def test_missing_default_reference_rejected():
    d = _base()
    d["nodes"][1]["next"] = "c"
    d["nodes"].append(
        {"id": "c", "type": "condition", "branches": [{"when": "x == 1", "next": "end"}],
         "default": "ghost"}
    )
    with pytest.raises(ValidationError) as exc:
        validate_definition(d)
    assert any("default" in m for m in exc.value.details)


def test_unreachable_node_rejected():
    d = _base()
    d["nodes"].append(
        {"id": "orphan", "type": "approval", "mode": "any",
         "assignees": ["y"], "next": "end"}
    )
    with pytest.raises(ValidationError) as exc:
        validate_definition(d)
    assert any("不可达" in m for m in exc.value.details)


def test_cycle_rejected():
    d = {
        "start_node": "start",
        "nodes": [
            {"id": "start", "type": "start", "next": "a"},
            {"id": "a", "type": "approval", "mode": "all", "assignees": ["x"], "next": "b"},
            {"id": "b", "type": "approval", "mode": "all", "assignees": ["y"], "next": "a"},
            {"id": "end", "type": "end"},
        ],
    }
    with pytest.raises(ValidationError) as exc:
        validate_definition(d)
    assert any("环路" in m for m in exc.value.details)


def test_duplicate_node_id_rejected():
    d = _base()
    d["nodes"].append({"id": "a", "type": "end"})
    with pytest.raises(ValidationError) as exc:
        validate_definition(d)
    assert any("重复" in m for m in exc.value.details)


def test_two_starts_rejected():
    d = _base()
    d["nodes"].append({"id": "start2", "type": "start", "next": "end"})
    with pytest.raises(ValidationError):
        validate_definition(d)


def test_empty_assignees_rejected():
    d = _base()
    d["nodes"][1]["assignees"] = []
    with pytest.raises(ValidationError):
        validate_definition(d)


def test_bad_timeout_rejected():
    d = _base()
    d["nodes"][1]["timeout"] = {"seconds": 0, "targets": ["z"]}
    with pytest.raises(ValidationError):
        validate_definition(d)


@pytest.mark.parametrize(
    "expr",
    [
        "__import__('os').system('id')",
        "x.f()",
        "x.__class__",
        "f(1)",
        "[i for i in x]",
        "x + 1",
        "x[0]",
        "lambda: 1",
        "x := 1",
    ],
)
def test_dangerous_expressions_rejected(expr):
    d = _base()
    d["nodes"][1]["next"] = "c"
    d["nodes"].append(
        {"id": "c", "type": "condition",
         "branches": [{"when": expr, "next": "end"}], "default": "end"}
    )
    with pytest.raises(ValidationError):
        validate_definition(d)


def test_safe_expression_accepted():
    d = _base()
    d["nodes"][1]["next"] = "c"
    d["nodes"].append(
        {"id": "c", "type": "condition",
         "branches": [
             {"when": "amount >= 10000 and level in ['A', 'B']", "next": "end"},
             {"when": "not flag", "next": "end"},
         ], "default": "end"}
    )
    validate_definition(d)
