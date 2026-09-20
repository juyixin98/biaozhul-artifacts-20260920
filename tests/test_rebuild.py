"""历史重建：结果与增量处理一致；重建期间新事件不丢失。"""

from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import delete, select

from app.models import ConsentEvent, ConsentState

from .conftest import grant_payload, withdraw_payload


def _states(db_session, org_id):
    rows = db_session.scalars(
        select(ConsentState).where(ConsentState.org_id == org_id)
    ).all()
    return {
        (str(s.subject_id), str(s.purpose_id)): {
            "version": s.version, "status": s.status,
            "last_event_seq": s.last_event_seq,
            "policy_version_id": s.policy_version_id,
            "expires_at": s.expires_at,
        }
        for s in rows
    }


def _fold_all(db_session, org_id):
    """测试侧独立折叠，作为重建结果的参照。"""
    events = db_session.scalars(
        select(ConsentEvent).where(ConsentEvent.org_id == org_id)
        .order_by(ConsentEvent.seq)
    ).all()
    states = {}
    for e in events:
        key = (str(e.subject_id), str(e.purpose_id))
        st = states.setdefault(key, {"version": 0, "status": "none",
                                     "last_event_seq": None,
                                     "policy_version_id": None, "expires_at": None})
        st["version"] += 1
        st["last_event_seq"] = e.seq
        if e.event_type == "grant":
            st["status"] = "granted"
            st["policy_version_id"] = e.policy_version_id
            st["expires_at"] = e.expires_at
        else:
            st["status"] = "withdrawn"
            st["expires_at"] = None
    return states


def _seed_events(client, admin, seeded, n_subjects=5):
    for i in range(n_subjects):
        ref = f"u{i}@x.test"
        client.post("/consents/events", headers=admin,
                    json=grant_payload(f"g{i}", ref, seeded["pv1"].id, 0))
        if i % 2 == 0:
            client.post("/consents/events", headers=admin,
                        json=withdraw_payload(f"w{i}", ref, 1))
            client.post("/consents/events", headers=admin,
                        json=grant_payload(f"g2-{i}", ref, seeded["pv1"].id, 2))


def test_rebuild_matches_incremental(client, admin, seeded, db_session):
    _seed_events(client, admin, seeded)
    org_id = seeded["org1"].id
    before = _states(db_session, org_id)

    # 清空投影后从事件历史重建
    db_session.execute(delete(ConsentState).where(ConsentState.org_id == org_id))
    db_session.commit()
    r = client.post("/consents/rebuild", headers=admin)
    assert r.status_code == 200, r.text

    after = _states(db_session, org_id)
    assert after == before
    assert after == _fold_all(db_session, org_id)


def test_rebuild_does_not_lose_concurrent_events(client, admin, seeded, db_session):
    _seed_events(client, admin, seeded, n_subjects=3)
    org_id = seeded["org1"].id

    def rebuild():
        return client.post("/consents/rebuild", headers=admin)

    def write_events():
        for i in range(10):
            client.post("/consents/events", headers=admin,
                        json=grant_payload(f"c{i}", f"c{i}@x.test",
                                           seeded["pv1"].id, 0))

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(rebuild)
        f2 = pool.submit(write_events)
        assert f1.result().status_code == 200
        f2.result()

    # 最终再做一次重建收敛，结果必须与全量折叠一致，且并发事件都在历史中
    client.post("/consents/rebuild", headers=admin)
    assert _states(db_session, org_id) == _fold_all(db_session, org_id)
    refs = {f"c{i}@x.test" for i in range(10)}
    for ref in refs:
        v = client.get("/consents/verify", headers=admin,
                       params={"subject_ref": ref, "purpose_code": "marketing"})
        assert v.status_code == 200 and v.json()["valid"] is True
