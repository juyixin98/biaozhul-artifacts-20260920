"""Demo seeding and end-to-end smoke test."""
from __future__ import annotations


def test_seed_demo_template_and_health(client):
    r = client.post("/demo/seed")
    assert r.status_code == 201
    # idempotent
    assert client.post("/demo/seed").status_code == 201

    tpl = client.get("/templates/expense").json()
    assert tpl["current_version"] == 1
    assert client.get("/health").json()["status"] == "ok"


def test_demo_flow_small_amount_completes(client, req_id):
    client.post("/demo/seed")
    r = client.post("/instances", json={
        "template_key": "expense", "submitter": "alice",
        "payload": {"amount": 100, "budget_status": "ok"},
    })
    assert r.status_code == 201
    inst = r.json()
    assert inst["current_node_id"] == "n_manager"

    for manager in ("manager1", "manager2"):
        r = client.post("/decisions", json={
            "request_id": req_id(), "instance_id": inst["id"], "expected_version": 1,
            "actor": manager, "decision": "approve",
        })
        assert r.status_code == 200
    final = client.get(f"/instances/{inst['id']}").json()
    assert final["status"] == "completed"


def test_demo_flow_large_amount_requires_director(client, req_id):
    client.post("/demo/seed")
    inst = client.post("/instances", json={
        "template_key": "expense", "submitter": "alice",
        "payload": {"amount": 99999},
    }).json()
    for manager in ("manager1", "manager2"):
        client.post("/decisions", json={
            "request_id": req_id(), "instance_id": inst["id"], "expected_version": 1,
            "actor": manager, "decision": "approve",
        })
    got = client.get(f"/instances/{inst['id']}").json()
    assert got["current_node_id"] == "n_director"
    assert {t["assignee"] for t in got["open_tasks"]} == {"director1", "director2"}
