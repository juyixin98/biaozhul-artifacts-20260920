"""邀请生命周期：8 分钟超时重排、并发接受唯一有效分配、重复请求幂等。"""

from datetime import datetime, timedelta, timezone

from conftest import generate, make_caregiver, make_plan, make_qualification

from app.models import Assignment, Offer

UTC = timezone.utc


def _setup_task(env, num_caregivers=2):
    client, clock, db = env
    cgs = []
    for i in range(num_caregivers):
        cg = make_caregiver(client, name=f"cg{i}")
        make_qualification(client, cg, "RN")
        cgs.append(cg)
    plan_id = make_plan(client)
    generate(client, plan_id)
    task_id = client.get("/tasks", params={"plan_id": plan_id}).json()[0]["id"]
    return client, clock, db, cgs, task_id


def test_accept_offer_happy_path_and_idempotent(env):
    client, clock, db, cgs, task_id = _setup_task(env)
    r = client.post(f"/tasks/{task_id}/offer", json={})
    assert r.status_code == 201
    offer = r.json()
    # 邀请 8 分钟后过期
    offered_at = datetime.fromisoformat(offer["offered_at"])
    expires_at = datetime.fromisoformat(offer["expires_at"])
    assert (expires_at - offered_at) == timedelta(minutes=8)

    r = client.post(f"/offers/{offer['id']}/accept",
                    json={"caregiver_id": offer["caregiver_id"]})
    assert r.status_code == 200
    assignment_id = r.json()["assignment"]["id"]
    assert client.get(f"/tasks/{task_id}").json()["status"] == "assigned"

    # 重复接受同一邀请：幂等返回同一分配，不重复占工时
    r = client.post(f"/offers/{offer['id']}/accept",
                    json={"caregiver_id": offer["caregiver_id"]})
    assert r.status_code == 200
    assert r.json()["assignment"]["id"] == assignment_id
    assert db.query(Assignment).filter_by(task_id=task_id).count() == 1


def test_accept_expired_offer_rejected(env):
    client, clock, db, cgs, task_id = _setup_task(env)
    offer = client.post(f"/tasks/{task_id}/offer", json={}).json()
    clock.advance(minutes=9)  # 超过 8 分钟 TTL
    r = client.post(f"/offers/{offer['id']}/accept",
                    json={"caregiver_id": offer["caregiver_id"]})
    assert r.status_code == 409
    assert db.query(Assignment).count() == 0


def test_offer_expires_and_reassigned_to_next_candidate(env):
    client, clock, db, cgs, task_id = _setup_task(env)
    offer1 = client.post(f"/tasks/{task_id}/offer", json={}).json()
    first_caregiver = offer1["caregiver_id"]

    clock.advance(minutes=9)
    r = client.post("/offers/expire")
    assert r.status_code == 200
    body = r.json()
    assert body["expired"] == 1
    assert body["results"][0]["expired_offer_id"] == offer1["id"]
    new_offer_id = body["results"][0]["new_offer_id"]
    assert new_offer_id is not None

    # 重新分配给了另一位候选人（让邀请过期者被排除）
    new_offer = db.get(Offer, new_offer_id)
    assert new_offer.caregiver_id != first_caregiver
    assert db.get(Offer, offer1["id"]).status == "expired"
    assert client.get(f"/tasks/{task_id}").json()["status"] == "offered"


def test_concurrent_accepts_keep_single_assignment(env):
    """多人并发接受：两个待响应邀请同时接受，只能保留一个有效分配。"""
    client, clock, db, cgs, task_id = _setup_task(env)
    offer1 = client.post(f"/tasks/{task_id}/offer",
                         json={"caregiver_id": cgs[0]}).json()
    # 直接插入第二个 pending 邀请，模拟并发场景下两个在途邀请
    now = clock.now()
    offer2 = Offer(task_id=task_id, caregiver_id=cgs[1], status="pending",
                   offered_at=now, expires_at=now + timedelta(minutes=8))
    db.add(offer2)
    db.commit()

    r1 = client.post(f"/offers/{offer1['id']}/accept",
                     json={"caregiver_id": cgs[0]})
    r2 = client.post(f"/offers/{offer2.id}/accept",
                     json={"caregiver_id": cgs[1]})
    # 恰好一个成功，另一个 409（任务已被占用）
    assert sorted([r1.status_code, r2.status_code]) == [200, 409]
    assignments = db.query(Assignment).filter_by(task_id=task_id).all()
    assert len(assignments) == 1  # 只有一个有效分配
    assert db.get(Offer, offer2.id if r1.status_code == 200
                  else offer1["id"]).status == "superseded"


def test_accept_after_task_assigned_fails(env):
    """超时任务与接受请求同时发生：任务已分配后，迟到的接受无效。"""
    client, clock, db, cgs, task_id = _setup_task(env)
    offer1 = client.post(f"/tasks/{task_id}/offer",
                         json={"caregiver_id": cgs[0]}).json()
    now = clock.now()
    offer2 = Offer(task_id=task_id, caregiver_id=cgs[1], status="pending",
                   offered_at=now, expires_at=now + timedelta(minutes=8))
    db.add(offer2)
    db.commit()
    db.refresh(offer2)

    client.post(f"/offers/{offer1['id']}/accept", json={"caregiver_id": cgs[0]})
    r = client.post(f"/offers/{offer2.id}/accept",
                    json={"caregiver_id": cgs[1]})
    assert r.status_code == 409
    assert db.query(Assignment).filter_by(task_id=task_id).count() == 1


def test_reject_triggers_reoffer(env):
    client, clock, db, cgs, task_id = _setup_task(env)
    offer = client.post(f"/tasks/{task_id}/offer", json={}).json()
    r = client.post(f"/offers/{offer['id']}/reject",
                    json={"caregiver_id": offer["caregiver_id"]})
    assert r.status_code == 200
    assert r.json()["new_offer_id"] is not None
    new_offer = db.get(Offer, r.json()["new_offer_id"])
    assert new_offer.caregiver_id != offer["caregiver_id"]
