"""认证、租户隔离与完整会话生命周期的端到端 API 测试。"""
from __future__ import annotations

from tests.conftest import SUPER_HEADERS


# ---------------------------------------------------------------- 认证

def test_super_admin_required(client):
    r = client.post("/admin/tenants", json={"name": "x"})
    assert r.status_code == 401
    r = client.post("/admin/tenants", json={"name": "x"},
                    headers={"X-Admin-Key": "wrong"})
    assert r.status_code == 401


def test_tenant_admin_required(client, make_tenant):
    t = make_tenant()
    assert client.post("/access-points", json={"name": "ap", "capacity": 1}).status_code == 401
    r = client.post("/access-points", json={"name": "ap", "capacity": 1},
                    headers={"X-Admin-Key": "nonsense"})
    assert r.status_code == 401
    assert t.headers != SUPER_HEADERS


def test_device_token_required(client, make_tenant, make_ap):
    t = make_tenant()
    ap = make_ap(t)
    r = client.post("/device/sessions/connect", json={"access_point_id": ap["id"]})
    assert r.status_code == 401
    r = client.post("/device/sessions/connect", json={"access_point_id": ap["id"]},
                    headers={"X-Device-Token": "bogus"})
    assert r.status_code == 401


# ---------------------------------------------------------------- 管理面基本流程

def test_admin_crud_flow(client, make_tenant, make_ap, make_pool, make_device):
    t = make_tenant("acme")
    ap = make_ap(t, capacity=10)
    pool = make_pool(t, ap["id"], "10.10.0.0/24", reserved_first=2, reserved_last=1)
    assert pool["assignable_count"] == 251  # 254 - 3 保留

    dev = make_device(t, "edge-1")
    assert dev["token"].startswith("dev_")

    # 列表只返回自己租户的资源
    aps = client.get("/access-points", headers=t.headers).json()
    assert [a["id"] for a in aps] == [ap["id"]]
    devices = client.get("/devices", headers=t.headers).json()
    assert devices[0]["name"] == "edge-1"


def test_pool_rejects_network_cidr_and_overlap(client, make_tenant, make_ap):
    t = make_tenant()
    ap = make_ap(t)
    r = client.post(f"/access-points/{ap['id']}/pools",
                    json={"cidr": "10.0.0.5/24"}, headers=t.headers)
    assert r.status_code == 422
    r = client.post(f"/access-points/{ap['id']}/pools",
                    json={"cidr": "10.0.0.0/24"}, headers=t.headers)
    assert r.status_code == 201
    r = client.post(f"/access-points/{ap['id']}/pools",
                    json={"cidr": "10.0.0.128/25"}, headers=t.headers)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "pool_overlap"


# ---------------------------------------------------------------- 会话生命周期

def _connect(client, device, ap_id):
    return client.post("/device/sessions/connect",
                       json={"access_point_id": ap_id}, headers=device["token_headers"])


def test_full_session_lifecycle_generation_and_idempotency(
        client, make_tenant, make_ap, make_pool, make_device):
    t = make_tenant()
    ap = make_ap(t, capacity=5)
    make_pool(t, ap["id"], "10.10.0.0/30")  # 仅 .1 .2 可用
    dev = make_device(t)

    r1 = _connect(client, dev, ap["id"])
    assert r1.status_code == 201, r1.text
    l1 = r1.json()
    assert l1["generation"] == 1
    assert l1["ip_address"] == "10.10.0.1"
    assert l1["reused"] is False

    # 重复连接幂等：同一条租约
    r2 = _connect(client, dev, ap["id"])
    assert r2.status_code == 201
    l2 = r2.json()
    assert l2["lease_id"] == l1["lease_id"]
    assert l2["generation"] == 1
    assert l2["reused"] is True

    # 当前会话查询
    cur = client.get("/device/sessions/current", headers=dev["token_headers"]).json()
    assert cur["lease_id"] == l1["lease_id"]

    # 心跳刷新 last_seen
    hb = client.post(f"/device/leases/{l1['lease_id']}/heartbeat",
                     json={"generation": 1}, headers=dev["token_headers"])
    assert hb.status_code == 200
    assert hb.json()["status"] == "active"

    # 错误代次心跳被拒
    r = client.post(f"/device/leases/{l1['lease_id']}/heartbeat",
                    json={"generation": 99}, headers=dev["token_headers"])
    assert r.status_code == 409
    assert r.json()["error"] == "stale_generation"

    # 关闭
    r = client.post(f"/device/leases/{l1['lease_id']}/close",
                    json={"generation": 1}, headers=dev["token_headers"])
    assert r.status_code == 200
    assert r.json()["status"] == "closed"
    assert r.json()["termination_reason"] == "closed"
    assert r.json()["ended_at"] is not None

    # 同代次重复关闭幂等
    r = client.post(f"/device/leases/{l1['lease_id']}/close",
                    json={"generation": 1}, headers=dev["token_headers"])
    assert r.status_code == 200
    assert r.json()["status"] == "closed"

    # 关闭后心跳不能复活
    r = client.post(f"/device/leases/{l1['lease_id']}/heartbeat",
                    json={"generation": 1}, headers=dev["token_headers"])
    assert r.status_code == 409
    assert r.json()["error"] == "lease_terminated"

    # 重连 = 新代次 + 新租约
    r3 = _connect(client, dev, ap["id"])
    assert r3.status_code == 201
    l3 = r3.json()
    assert l3["lease_id"] != l1["lease_id"]
    assert l3["generation"] == 2
    assert l3["reused"] is False

    # 旧代次关闭不能影响新会话
    r = client.post(f"/device/leases/{l3['lease_id']}/close",
                    json={"generation": 1}, headers=dev["token_headers"])
    assert r.status_code == 409
    cur = client.get("/device/sessions/current", headers=dev["token_headers"]).json()
    assert cur["lease_id"] == l3["lease_id"]
    assert cur["status"] == "active"


# ---------------------------------------------------------------- 租户隔离

def test_strict_tenant_isolation(client, make_tenant, make_ap, make_pool, make_device):
    a = make_tenant("alpha")
    b = make_tenant("beta")
    ap_a = make_ap(a, name="ap-a")
    make_pool(a, ap_a["id"], "10.10.0.0/30")
    dev_a = make_device(a, "dev-a")
    ap_b = make_ap(b, name="ap-b")
    make_pool(b, ap_b["id"], "10.10.0.0/30")
    dev_b = make_device(b, "dev-b")

    lease_a = _connect(client, dev_a, ap_a["id"]).json()

    # B 的管理员看不到也操作不了 A 的任何资源（统一 404，不泄露存在性）
    assert client.post(f"/access-points/{ap_a['id']}/pools",
                       json={"cidr": "10.9.0.0/24"}, headers=b.headers).status_code == 404
    assert client.get(f"/access-points/{ap_a['id']}/pools", headers=b.headers).status_code == 404
    assert client.post(f"/devices/{dev_a['id']}/revoke", headers=b.headers).status_code == 404
    assert client.get(f"/leases/{lease_a['lease_id']}", headers=b.headers).status_code == 404
    assert client.get(f"/leases/{lease_a['lease_id']}/events",
                      headers=b.headers).status_code == 404

    leases_b = client.get("/leases", headers=b.headers).json()
    assert leases_b == []

    # B 的设备无法连 A 的接入点
    r = _connect(client, dev_b, ap_a["id"])
    assert r.status_code == 404
    # B 的设备无法给 A 的租约发心跳
    r = client.post(f"/device/leases/{lease_a['lease_id']}/heartbeat",
                    json={"generation": 1}, headers=dev_b["token_headers"])
    assert r.status_code == 404
    r = client.post(f"/device/leases/{lease_a['lease_id']}/close",
                    json={"generation": 1}, headers=dev_b["token_headers"])
    assert r.status_code == 404


def test_same_cidr_in_different_tenants_is_independent(
        client, make_tenant, make_ap, make_pool, make_device):
    """部分唯一索引以接入点为作用域：两个租户可以各自分配相同的 IP。"""
    a = make_tenant("alpha2")
    b = make_tenant("beta2")
    ap_a = make_ap(a)
    ap_b = make_ap(b)
    make_pool(a, ap_a["id"], "172.16.0.0/30")
    make_pool(b, ap_b["id"], "172.16.0.0/30")
    dev_a = make_device(a)
    dev_b = make_device(b)

    la = _connect(client, dev_a, ap_a["id"]).json()
    lb = _connect(client, dev_b, ap_b["id"]).json()
    assert la["ip_address"] == lb["ip_address"] == "172.16.0.1"
    assert la["lease_id"] != lb["lease_id"]


def test_revoked_device_token_rejected_and_session_killed(
        client, make_tenant, make_ap, make_pool, make_device):
    t = make_tenant()
    ap = make_ap(t)
    make_pool(t, ap["id"], "10.10.0.0/30")
    dev = make_device(t)
    lease = _connect(client, dev, ap["id"]).json()

    r = client.post(f"/devices/{dev['id']}/revoke", headers=t.headers)
    assert r.status_code == 200
    assert r.json()["status"] == "revoked"

    # 旧令牌立刻失效，无法再连接（不能靠旧令牌重新接入）
    r = _connect(client, dev, ap["id"])
    assert r.status_code == 403
    assert r.json()["detail"]["error"] == "device_revoked"

    # 活动会话已被终止并释放
    info = client.get(f"/leases/{lease['lease_id']}", headers=t.headers).json()
    assert info["status"] == "revoked"
    assert info["termination_reason"] == "device_revoked"
    assert info["ended_at"] is not None
    assert client.get("/device/sessions/current",
                      headers=dev["token_headers"]).status_code == 403

    # 撤销幂等
    assert client.post(f"/devices/{dev['id']}/revoke", headers=t.headers).status_code == 200
