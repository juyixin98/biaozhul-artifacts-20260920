"""授予/撤回/到期/验证的基本行为与幂等、冲突语义。"""

import time
from datetime import datetime, timedelta, timezone

from .conftest import grant_payload, withdraw_payload


def _verify(client, headers, subject_ref):
    return client.get("/consents/verify", headers=headers,
                      params={"subject_ref": subject_ref, "purpose_code": "marketing"})


def test_grant_and_verify(client, admin, auditor, seeded):
    r = client.post("/consents/events", headers=admin,
                    json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))
    assert r.status_code == 201, r.text
    assert r.json()["resulting_version"] == 1

    v = _verify(client, auditor, "u1@x.test")
    assert v.status_code == 200
    body = v.json()
    assert body["valid"] is True
    assert body["status"] == "granted"
    assert body["basis_event"]["event_id"] == "e1"
    assert body["policy_version"]["version"] == 1
    assert body["policy_stale"] is False


def test_withdraw_invalidates(client, admin, auditor, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))
    r = client.post("/consents/events", headers=admin,
                    json=withdraw_payload("e2", "u1@x.test", 1))
    assert r.status_code == 201
    v = _verify(client, auditor, "u1@x.test").json()
    assert v["valid"] is False
    assert v["status"] == "withdrawn"
    assert v["basis_event"]["event_id"] == "e2"


def test_delayed_retry_of_old_grant_cannot_revive(client, admin, auditor, seeded):
    """撤回后，旧授予的延迟重试不能恢复授权；只有新的明确授予可以。"""
    payload = grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0)
    client.post("/consents/events", headers=admin, json=payload)
    client.post("/consents/events", headers=admin,
                json=withdraw_payload("e2", "u1@x.test", 1))

    # 同一 event_id 重试 -> 返回原结果，不改变当前状态
    r = client.post("/consents/events", headers=admin, json=payload)
    assert r.status_code == 201
    assert r.json()["replayed"] is True
    assert r.json()["resulting_version"] == 1
    assert _verify(client, auditor, "u1@x.test").json()["valid"] is False

    # 不同 event_id 但携带过期 expected_version -> 冲突
    r = client.post("/consents/events", headers=admin,
                    json=grant_payload("e1b", "u1@x.test", seeded["pv1"].id, 0))
    assert r.status_code == 409
    assert _verify(client, auditor, "u1@x.test").json()["valid"] is False

    # 新的明确授予（基于当前版本）可以恢复
    r = client.post("/consents/events", headers=admin,
                    json=grant_payload("e3", "u1@x.test", seeded["pv1"].id, 2))
    assert r.status_code == 201
    assert _verify(client, auditor, "u1@x.test").json()["valid"] is True


def test_duplicate_request_returns_original_result(client, admin, seeded):
    payload = grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0)
    first = client.post("/consents/events", headers=admin, json=payload).json()
    second = client.post("/consents/events", headers=admin, json=payload).json()
    assert second["replayed"] is True
    assert second["seq"] == first["seq"]
    assert second["resulting_version"] == first["resulting_version"]


def test_same_event_id_different_content_conflicts(client, admin, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))
    r = client.post("/consents/events", headers=admin,
                    json=grant_payload("e1", "u2@x.test", seeded["pv1"].id, 0))
    assert r.status_code == 409


def test_stale_expected_version_conflicts(client, admin, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))
    r = client.post("/consents/events", headers=admin,
                    json=withdraw_payload("e2", "u1@x.test", 0))
    assert r.status_code == 409


def test_failed_event_not_in_history(client, admin, auditor, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))
    # 版本冲突的失败事件
    r = client.post("/consents/events", headers=admin,
                    json=withdraw_payload("e2", "u1@x.test", 5))
    assert r.status_code == 409
    h = client.get("/consents/history", headers=auditor,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"})
    events = h.json()["events"]
    assert [e["event_id"] for e in events] == ["e1"]


def test_expiry_boundary(client, admin, auditor, seeded):
    """到期立即失效，不依赖清理任务。"""
    expires = datetime.now(timezone.utc) + timedelta(seconds=1)
    r = client.post("/consents/events", headers=admin,
                    json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0,
                                       expires_at=expires.isoformat()))
    assert r.status_code == 201
    assert _verify(client, auditor, "u1@x.test").json()["valid"] is True
    time.sleep(1.3)
    v = _verify(client, auditor, "u1@x.test").json()
    assert v["valid"] is False
    assert v["expired"] is True
    assert v["status"] == "granted"  # 状态未变，只是已过期


def test_grant_with_unpublished_policy_rejected(client, admin, seeded):
    r = client.post(f"/purposes/marketing/policy-versions", headers=admin,
                    json={"version": 9, "content": "draft"})
    draft_id = r.json()["id"]
    r = client.post("/consents/events", headers=admin,
                    json=grant_payload("e1", "u1@x.test", draft_id, 0))
    assert r.status_code == 422


def test_auditor_cannot_write(client, auditor, seeded):
    r = client.post("/consents/events", headers=auditor,
                    json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))
    assert r.status_code == 403


def test_cross_org_isolation(client, admin, org2_admin, auditor, seeded):
    client.post("/consents/events", headers=admin,
                json=grant_payload("e1", "u1@x.test", seeded["pv1"].id, 0))
    # org2 的管理员查不到 org1 的主体
    r = client.get("/consents/verify", headers=org2_admin,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"})
    assert r.status_code == 404
    # org2 看不到 org1 的 purpose
    r = client.get("/consents/verify", headers=org2_admin,
                   params={"subject_ref": "u1@x.test", "purpose_code": "marketing"})
    assert r.status_code == 404
