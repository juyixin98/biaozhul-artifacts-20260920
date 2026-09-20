"""Condition branches and the restricted expression language."""
from __future__ import annotations

import pytest

from app.expression import ExpressionError, evaluate
from tests.helpers import condition_def, create_published, decide, start


def test_expression_basic_operators():
    assert evaluate("amount >= 10000", {"amount": 20000}) is True
    assert evaluate("amount >= 10000", {"amount": 100}) is False
    assert evaluate("form.region == 'EU' and amount > 5", {"form": {"region": "EU"}, "amount": 6})
    assert evaluate("x in [1, 2, 3]", {"x": 2})
    assert evaluate("not flagged", {"flagged": False})
    assert evaluate("len(tags) > 1", {"tags": ["a", "b", "c"]})
    assert evaluate("lower(name) == 'ok'", {"name": "OK"})
    assert evaluate("missing == null", {}) is True
    assert evaluate("missing > 3", {}) is False


def test_expression_blocks_dangerous_inputs():
    dangerous = [
        "__import__('os')",
        "x.__class__",
        "getattr(x, 'a')",
        "open('/etc/passwd')",
        "x = 1",
        "lambda: 1",
        "1 if True else 2",
        "foo()",  # unlisted function
    ]
    for src in dangerous:
        with pytest.raises(ExpressionError):
            evaluate(src, {"x": 1})


def test_branch_routes_to_big_approval(client, req_id):
    create_published(client, "cond", condition_def())
    inst = start(client, "cond", payload={"amount": 50000})
    assert inst["current_node_id"] == "big"
    assert inst["open_tasks"][0]["assignee"] == "boss"

    r = decide(client, instance_id=inst["id"], version=1, actor="boss",
               decision="approve", request_id=req_id())
    assert r.status_code == 200
    assert r.json()["instance"]["status"] == "completed"


def test_default_branch_for_small_amount(client, req_id):
    create_published(client, "cond2", condition_def())
    inst = start(client, "cond2", payload={"amount": 50})
    assert inst["current_node_id"] == "small"
    assert inst["open_tasks"][0]["assignee"] == "clerk"
    r = decide(client, instance_id=inst["id"], version=1, actor="clerk",
               decision="approve", request_id=req_id())
    assert r.json()["instance"]["status"] == "completed"


def test_history_contains_branch_record(client):
    create_published(client, "cond3", condition_def())
    inst = start(client, "cond3", payload={"amount": 50})
    actions = [h["action"] for h in client.get(f"/instances/{inst['id']}/history").json()["history"]]
    assert "branch_taken" in actions
