"""批量导入：最多 500 条、整批回滚、重复导入幂等。"""

from .conftest import grant_payload


def _batch(client, admin, items):
    return client.post("/consents/events/batch", headers=admin, json={"items": items})


def test_batch_limit_500(client, admin, seeded):
    items = [grant_payload(f"e{i}", f"u{i}@x.test", seeded["pv1"].id, 0)
             for i in range(501)]
    r = _batch(client, admin, items)
    assert r.status_code == 422


def test_batch_success(client, admin, auditor, seeded):
    items = [grant_payload(f"e{i}", f"u{i}@x.test", seeded["pv1"].id, 0)
             for i in range(10)]
    r = _batch(client, admin, items)
    assert r.status_code == 201, r.text
    assert len(r.json()["results"]) == 10
    v = client.get("/consents/verify", headers=auditor,
                   params={"subject_ref": "u5@x.test", "purpose_code": "marketing"}).json()
    assert v["valid"] is True


def test_batch_rolls_back_on_any_error(client, admin, auditor, seeded):
    items = [grant_payload(f"e{i}", f"u{i}@x.test", seeded["pv1"].id, 0) for i in range(3)]
    # 第三条 expected_version 错误 -> 整批失败
    items[2]["expected_version"] = 7
    r = _batch(client, admin, items)
    assert r.status_code == 409
    # 前两条也不能落库
    v = client.get("/consents/verify", headers=auditor,
                   params={"subject_ref": "u0@x.test", "purpose_code": "marketing"})
    assert v.status_code == 404
    h = client.get("/consents/history", headers=auditor,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"})
    assert h.status_code == 404


def test_duplicate_import_is_idempotent(client, admin, auditor, seeded):
    items = [grant_payload(f"e{i}", f"u{i}@x.test", seeded["pv1"].id, 0) for i in range(3)]
    first = _batch(client, admin, items)
    assert first.status_code == 201
    second = _batch(client, admin, items)
    assert second.status_code == 201
    assert all(r["replayed"] for r in second.json()["results"])
    v = client.get("/consents/verify", headers=auditor,
                   params={"subject_ref": "u0@x.test", "purpose_code": "marketing"}).json()
    assert v["state_version"] == 1  # 没有重复应用


def test_batch_conflicting_event_id_rolls_back(client, admin, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e0", "u0@x.test", seeded["pv1"].id, 0))
    # 批量中重用 e0 但内容不同 -> 冲突，整批回滚
    items = [grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0),
             grant_payload("e0", "u9@x.test", seeded["pv1"].id, 0)]
    r = _batch(client, admin, items)
    assert r.status_code == 409
    v = client.get("/consents/verify", headers=admin,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"})
    assert v.status_code == 404
