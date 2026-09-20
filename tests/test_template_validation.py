"""Template validation tests: references, reachability, cycles, expressions."""
from __future__ import annotations

from tests.helpers import create_published


def test_happy_path_definition_publishes(client):
    from tests.helpers import simple_chain_def

    r = client.post(
        "/templates",
        json={"key": "ok", "name": "t", "definition": simple_chain_def()},
    )
    assert r.status_code == 201


def test_missing_edge_reference_rejected(client):
    definition = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "ap", "type": "approval", "mode": "all", "assignees": ["a"]},
            {"id": "ok", "type": "end", "terminal": "approved"},
        ],
        "edges": [
            {"source": "s", "target": "ap"},
            {"source": "ap", "target": "ghost"},
        ],
    }
    r = client.post("/templates", json={"key": "bad", "name": "t", "definition": definition})
    assert r.status_code == 400
    codes = {i["code"] for i in r.json()["error"]["details"]}
    assert "unknown_edge_target" in codes


def test_unreachable_node_rejected(client):
    definition = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "ap", "type": "approval", "mode": "all", "assignees": ["a"]},
            {"id": "orphan", "type": "approval", "mode": "any", "assignees": ["b"]},
            {"id": "ok", "type": "end", "terminal": "approved"},
        ],
        "edges": [
            {"source": "s", "target": "ap"},
            {"source": "ap", "target": "ok"},
            {"source": "orphan", "target": "ok"},
        ],
    }
    r = client.post("/templates", json={"key": "unreach", "name": "t", "definition": definition})
    assert r.status_code == 400
    codes = {i["code"] for i in r.json()["error"]["details"]}
    assert "unreachable_node" in codes


def test_self_loop_and_cycle_rejected(client):
    definition = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "ap", "type": "approval", "mode": "all", "assignees": ["a"]},
            {"id": "ok", "type": "end", "terminal": "approved"},
        ],
        "edges": [
            {"source": "s", "target": "ap"},
            {"source": "ap", "target": "ap"},
            {"source": "ap", "target": "ok"},
        ],
    }
    r = client.post("/templates", json={"key": "cycle", "name": "t", "definition": definition})
    assert r.status_code == 400
    codes = {i["code"] for i in r.json()["error"]["details"]}
    assert "cycle_detected" in codes


def test_two_start_nodes_rejected(client):
    definition = {
        "nodes": [
            {"id": "s1", "type": "start"},
            {"id": "s2", "type": "start"},
            {"id": "ok", "type": "end", "terminal": "approved"},
        ],
        "edges": [
            {"source": "s1", "target": "ok"},
            {"source": "s2", "target": "ok"},
        ],
    }
    r = client.post("/templates", json={"key": "two-start", "name": "t", "definition": definition})
    assert r.status_code == 400
    assert r.json()["error"]["details"][0]["code"] == "start_cardinality"


def test_approval_requires_assignees(client):
    definition = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "ap", "type": "approval", "mode": "all"},
            {"id": "ok", "type": "end", "terminal": "approved"},
        ],
        "edges": [{"source": "s", "target": "ap"}, {"source": "ap", "target": "ok"}],
    }
    r = client.post("/templates", json={"key": "no-assignee", "name": "t", "definition": definition})
    assert r.status_code == 400
    codes = {i["code"] for i in r.json()["error"]["details"]}
    assert "approval_assignees_required" in codes


def test_condition_requires_default_edge(client):
    definition = {
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "c", "type": "condition"},
            {"id": "ok", "type": "end", "terminal": "approved"},
        ],
        "edges": [
            {"source": "s", "target": "c"},
            {"source": "c", "target": "ok", "expression": "x == 1"},
        ],
    }
    r = client.post("/templates", json={"key": "no-default", "name": "t", "definition": definition})
    assert r.status_code == 400
    codes = {i["code"] for i in r.json()["error"]["details"]}
    assert "condition_default" in codes


def test_dangerous_expression_rejected_at_publish(client):
    bad_expressions = [
        "__import__('os').system('echo pwned')",
        "x.__class__.__mro__",
        "eval('1+1')",
        "x if True else y",
    ]
    for i, expr in enumerate(bad_expressions):
        definition = {
            "nodes": [
                {"id": "s", "type": "start"},
                {"id": "c", "type": "condition"},
                {"id": "ap", "type": "approval", "mode": "any", "assignees": ["a"]},
                {"id": "ok", "type": "end", "terminal": "approved"},
            ],
            "edges": [
                {"source": "s", "target": "c"},
                {"source": "c", "target": "ap", "expression": expr},
                {"source": "c", "target": "ok"},  # default, keeps graph acyclic
                {"source": "ap", "target": "ok"},
            ],
        }
        r = client.post(
            "/templates", json={"key": f"danger-{i}", "name": "t", "definition": definition}
        )
        assert r.status_code == 400, expr
        codes = {issue["code"] for issue in r.json()["error"]["details"]}
        assert "invalid_expression" in codes


def test_published_version_is_immutable(client):
    from tests.helpers import simple_chain_def

    create_published(client, "imm", simple_chain_def())
    r = client.post(
        "/templates/imm/versions",
        json={"definition": simple_chain_def(assignees=("z",))},
    )
    # A new draft can be created, but version 1's content cannot be touched:
    assert r.status_code == 201
    v1 = client.get("/templates/imm/versions/1").json()
    assert v1["status"] == "published"
    assert [n for n in v1["definition"]["nodes"] if n["type"] == "approval"][0]["assignees"] == [
        "a1",
        "a2",
    ]


def test_duplicate_template_key_conflicts(client):
    from tests.helpers import simple_chain_def

    create_published(client, "dup", simple_chain_def())
    r = client.post(
        "/templates", json={"key": "dup", "name": "t", "definition": simple_chain_def()}
    )
    assert r.status_code == 409
