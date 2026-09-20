"""人工调整：授权单元校验、同一套约束检查、原因与变更历史记录。"""

from conftest import generate, make_caregiver, make_plan, make_qualification

from app.models import AdjustmentLog


def _make_coordinator(client, name, units):
    r = client.post("/coordinators", json={"name": name, "unit_ids": units})
    assert r.status_code == 201
    return r.json()["id"]


def test_manual_adjust_requires_authorized_unit(env):
    client, clock, db = env
    cg = make_caregiver(client)
    make_qualification(client, cg, "RN")
    plan_id = make_plan(client)
    generate(client, plan_id)
    task_id = client.get("/tasks", params={"plan_id": plan_id}).json()[0]["id"]

    outsider = _make_coordinator(client, "outsider", ["unit-b"])
    r = client.post(f"/tasks/{task_id}/adjust",
                    json={"coordinator_id": outsider, "caregiver_id": cg,
                          "reason": "try"})
    assert r.status_code == 403

    insider = _make_coordinator(client, "insider", ["unit-a"])
    r = client.post(f"/tasks/{task_id}/adjust",
                    json={"coordinator_id": insider, "caregiver_id": cg,
                          "reason": "coverage gap"})
    assert r.status_code == 200, r.text
    assert r.json()["source"] == "manual"
    assert client.get(f"/tasks/{task_id}").json()["status"] == "assigned"

    # 变更历史记录了原因与操作者
    history = client.get(f"/tasks/{task_id}/history").json()
    manual = [h for h in history if h["action"] == "manual_adjust"]
    assert len(manual) == 1
    assert manual[0]["reason"] == "coverage gap"
    assert manual[0]["actor"] == f"coordinator:{insider}"
    assert manual[0]["details"]["new_caregiver_id"] == cg


def test_manual_adjust_uses_same_constraints(env):
    client, clock, db = env
    cg = make_caregiver(client)  # 无资格
    coord = _make_coordinator(client, "coord", ["unit-a"])
    plan_id = make_plan(client)
    generate(client, plan_id)
    task_id = client.get("/tasks", params={"plan_id": plan_id}).json()[0]["id"]

    r = client.post(f"/tasks/{task_id}/adjust",
                    json={"coordinator_id": coord, "caregiver_id": cg,
                          "reason": "forced"})
    assert r.status_code == 422
    assert any("qualification" in reason
               for reason in r.json()["violations"][0]["reasons"])
    assert client.get(f"/tasks/{task_id}").json()["status"] == "pending"


def test_manual_adjust_reassign_records_old_and_new(env):
    client, clock, db = env
    cg1 = make_caregiver(client, name="one")
    cg2 = make_caregiver(client, name="two")
    make_qualification(client, cg1, "RN")
    make_qualification(client, cg2, "RN")
    coord = _make_coordinator(client, "coord", ["unit-a"])
    plan_id = make_plan(client)
    generate(client, plan_id)
    task_id = client.get("/tasks", params={"plan_id": plan_id}).json()[0]["id"]

    client.post(f"/tasks/{task_id}/adjust",
                json={"coordinator_id": coord, "caregiver_id": cg1,
                      "reason": "initial"})
    r = client.post(f"/tasks/{task_id}/adjust",
                    json={"coordinator_id": coord, "caregiver_id": cg2,
                          "reason": "swap"})
    assert r.status_code == 200
    assert r.json()["caregiver_id"] == cg2

    history = [h for h in client.get(f"/tasks/{task_id}/history").json()
               if h["action"] == "manual_adjust"]
    assert history[-1]["details"]["old_caregiver_id"] == cg1
    assert history[-1]["details"]["new_caregiver_id"] == cg2
    # 仍然只有一条有效分配
    from app.models import Assignment
    assert db.query(Assignment).filter_by(task_id=task_id).count() == 1
