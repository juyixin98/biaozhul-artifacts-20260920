"""政策版本：发布后不可修改、授予绑定具体版本、更新政策不自动续授权。"""

from .conftest import grant_payload


def test_policy_version_immutable_after_publish(client, admin, seeded):
    pv_id = str(seeded["pv1"].id)
    for method in ("put", "patch", "delete"):
        kwargs = {"json": {"content": "tampered"}} if method != "delete" else {}
        r = getattr(client, method)(f"/policy-versions/{pv_id}", headers=admin, **kwargs)
        assert r.status_code == 405
    # 重复发布 -> 冲突
    r = client.post(f"/policy-versions/{pv_id}/publish", headers=admin)
    assert r.status_code == 409


def test_grant_binds_specific_version_and_update_does_not_renew(client, admin, auditor, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))

    # 发布 v2
    r = client.post("/purposes/marketing/policy-versions", headers=admin,
                    json={"version": 2, "content": "v2"})
    pv2_id = r.json()["id"]
    client.post(f"/policy-versions/{pv2_id}/publish", headers=admin)

    v = client.get("/consents/verify", headers=auditor,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"}).json()
    # 授权仍绑定 v1，未被自动续到 v2
    assert v["valid"] is True
    assert v["policy_version"]["version"] == 1
    assert v["latest_policy_version"] == 2
    assert v["policy_stale"] is True
    assert v["state_version"] == 1  # 政策更新不产生新授权事件

    h = client.get("/consents/history", headers=auditor,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"}).json()
    assert len(h["events"]) == 1  # 历史中没有因政策更新而新增的授予
