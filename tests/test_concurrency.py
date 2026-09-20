"""并发写入：同一状态的并发撤回只有一个成功；乐观锁防止覆盖。"""

from concurrent.futures import ThreadPoolExecutor

from .conftest import grant_payload, withdraw_payload


def test_concurrent_withdraw_only_one_succeeds(client, admin, auditor, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("g1", "u1@x.test", seeded["pv1"].id, 0))

    def withdraw(event_id):
        return client.post("/consents/events", headers=admin,
                           json=withdraw_payload(event_id, "u1@x.test", 1))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(withdraw, ["w1", "w2"]))

    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409]

    h = client.get("/consents/history", headers=auditor,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"}).json()
    assert len(h["events"]) == 2  # 只有成功的那个进入历史
    v = client.get("/consents/verify", headers=auditor,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"}).json()
    assert v["valid"] is False
    assert v["state_version"] == 2


def test_concurrent_grants_on_same_key(client, admin, auditor, seeded):
    def grant(event_id):
        return client.post("/consents/events", headers=admin,
                           json=grant_payload(event_id, "u1@x.test",
                                              seeded["pv1"].id, 0))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(grant, [f"g{i}" for i in range(4)]))

    codes = sorted(r.status_code for r in results)
    assert codes == [201, 409, 409, 409]
    v = client.get("/consents/verify", headers=auditor,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"}).json()
    assert v["state_version"] == 1
