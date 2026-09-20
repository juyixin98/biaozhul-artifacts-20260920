"""并发与租约语义测试。

直接调用 lease_service，用真实 PostgreSQL + 多线程制造竞争，
覆盖：容量竞争、地址耗尽、重复 IP 防护、撤销竞争、迟到心跳、
代次校验、回收幂等与重启恢复。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text

from app import lease_service
from app.config import settings
from app.db import SessionLocal
from app.models import (
    AccessPoint,
    AddressPool,
    Device,
    Lease,
    LeaseEvent,
    LeaseStatus,
    Tenant,
)
from app.security import generate_token, hash_token


# ---------------------------------------------------------------- ORM 工厂

def _seed_tenant(name: str) -> str:
    db = SessionLocal()
    t = Tenant(name=name, admin_key_hash=hash_token(generate_token("adm")))
    db.add(t)
    db.commit()
    tid = t.id
    db.close()
    return str(tid)


def _seed_ap(tenant_id: str, capacity: int, name: str | None = None) -> str:
    db = SessionLocal()
    ap = AccessPoint(tenant_id=tenant_id, name=name or f"ap-{time.time_ns()}", capacity=capacity)
    db.add(ap)
    db.commit()
    aid = ap.id
    db.close()
    return str(aid)


def _seed_pool(ap_id: str, tenant_id: str, cidr: str, rf: int = 0, rl: int = 0) -> None:
    db = SessionLocal()
    db.add(AddressPool(tenant_id=tenant_id, access_point_id=ap_id, cidr=cidr,
                       reserved_first=rf, reserved_last=rl))
    db.commit()
    db.close()


def _seed_device(tenant_id: str, name: str | None = None) -> str:
    db = SessionLocal()
    d = Device(tenant_id=tenant_id, name=name or f"dev-{time.time_ns()}",
               token_hash=hash_token(generate_token("dev")))
    db.add(d)
    db.commit()
    did = d.id
    db.close()
    return str(did)


def _active_leases(ap_id: str) -> int:
    db = SessionLocal()
    n = db.execute(
        select(func.count()).select_from(Lease).where(
            Lease.access_point_id == ap_id, Lease.status == LeaseStatus.active)
    ).scalar_one()
    db.close()
    return n


def _active_ips(ap_id: str) -> set[str]:
    db = SessionLocal()
    rows = db.execute(select(Lease.ip_address).where(
        Lease.access_point_id == ap_id, Lease.status == LeaseStatus.active)).all()
    db.close()
    return {str(ip) for (ip,) in rows}


def _device_active_lease(device_id: str):
    db = SessionLocal()
    l = db.execute(select(Lease).where(
        Lease.device_id == device_id, Lease.status == LeaseStatus.active)).scalar_one_or_none()
    out = (str(l.id), l.generation, str(l.ip_address)) if l else None
    db.close()
    return out


def _terminated_event_count(lease_id: str) -> int:
    db = SessionLocal()
    n = db.execute(select(func.count()).select_from(LeaseEvent).where(
        LeaseEvent.lease_id == lease_id, LeaseEvent.event_type == "terminated")).scalar_one()
    db.close()
    return n


def _set_last_seen(lease_id: str, delta_seconds: float) -> None:
    from app.models import utcnow
    db = SessionLocal()
    db.execute(text("UPDATE leases SET last_seen_at = :t WHERE id = :i"),
               {"t": utcnow() - timedelta(seconds=delta_seconds), "i": lease_id})
    db.commit()
    db.close()


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(settings, "heartbeat_timeout_seconds", 1)
    return 1


# ---------------------------------------------------------------- 建立会话：幂等 / 容量 / 地址

def test_concurrent_connect_same_device_is_idempotent():
    tid = _seed_tenant("c-idem")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.20.0.0/29")  # 6 个可分配地址
    dev = _seed_device(tid)

    barrier = threading.Barrier(20)

    def connect():
        barrier.wait()
        return lease_service.establish_session(dev, ap)

    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = [ex.submit(connect) for _ in range(20)]
        results = [f.result() for f in futures]

    leases = {(r.lease["lease_id"], r.lease["generation"], r.lease["ip_address"]) for r in results}
    assert len(leases) == 1  # 全部收敛到同一条活动租约
    assert _active_leases(ap) == 1
    reused = sum(1 for r in results if r.reused)
    assert reused == 19


def test_capacity_contention_never_exceeds():
    tid = _seed_tenant("c-cap")
    cap = 5
    ap = _seed_ap(tid, capacity=cap)
    _seed_pool(ap, tid, "10.21.0.0/24")  # 地址远多于容量
    devices = [_seed_device(tid) for _ in range(50)]

    barrier = threading.Barrier(50)

    def connect(dev):
        barrier.wait()
        try:
            return ("ok", lease_service.establish_session(dev, ap))
        except lease_service.ServiceError as e:
            return (e.code, None)

    with ThreadPoolExecutor(max_workers=50) as ex:
        results = list(ex.map(connect, devices))

    ok = [r for r in results if r[0] == "ok"]
    full = [r for r in results if r[0] == "ap_full"]
    assert len(ok) == cap
    assert len(full) == 50 - cap
    assert _active_leases(ap) == cap
    # 容量内的 IP 全部唯一
    assert len(_active_ips(ap)) == cap


def test_address_pool_exhaustion_concurrent():
    tid = _seed_tenant("c-exh")
    ap = _seed_ap(tid, capacity=100)  # 容量充足，瓶颈在地址池
    _seed_pool(ap, tid, "10.22.0.0/30", rf=0, rl=0)  # 仅 .1/.2 两个
    devices = [_seed_device(tid) for _ in range(12)]

    barrier = threading.Barrier(12)

    def connect(dev):
        barrier.wait()
        try:
            return ("ok", lease_service.establish_session(dev, ap))
        except lease_service.ServiceError as e:
            return (e.code, None)

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(connect, devices))

    ok = [r for r in results if r[0] == "ok"]
    exhausted = [r for r in results if r[0] == "address_pool_exhausted"]
    assert len(ok) == 2
    assert len(exhausted) == 10
    assert _active_ips(ap) == {"10.22.0.1", "10.22.0.2"}


def test_network_broadcast_reserved_never_assigned_through_service():
    tid = _seed_tenant("c-res")
    ap = _seed_ap(tid, capacity=100)
    _seed_pool(ap, tid, "10.23.0.0/29", rf=1, rl=1)  # 可分配仅 .2-.5
    devices = [_seed_device(tid) for _ in range(6)]
    for d in devices[:4]:
        lease_service.establish_session(d, ap)
    assert _active_ips(ap) == {f"10.23.0.{i}" for i in range(2, 6)}
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.establish_session(devices[4], ap)
    assert ei.value.code == "address_pool_exhausted"
    # 被排除的地址：.0 网络、.7 广播、.1/.6 保留
    assert _active_ips(ap).isdisjoint({"10.23.0.0", "10.23.0.7", "10.23.0.1", "10.23.0.6"})


def test_ip_released_after_close_can_be_reassigned():
    tid = _seed_tenant("c-rel")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.24.0.0/30")  # .1 .2
    d1, d2, d3 = _seed_device(tid), _seed_device(tid), _seed_device(tid)

    l1 = lease_service.establish_session(d1, ap).lease
    l2 = lease_service.establish_session(d2, ap).lease
    assert {l1["ip_address"], l2["ip_address"]} == {"10.24.0.1", "10.24.0.2"}
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.establish_session(d3, ap)
    assert ei.value.code == "address_pool_exhausted"

    lease_service.close_session(d1, l1["lease_id"], l1["generation"])
    l3 = lease_service.establish_session(d3, ap).lease
    assert l3["ip_address"] == l1["ip_address"]  # 释放后可重新分配
    assert _active_leases(ap) == 2


def test_concurrent_close_and_reconnect_single_active_lease():
    """关闭旧会话与重连真正并发：无论线程如何交错，最终恰有一条活动租约，
    旧租约已关闭，新租约代次 +1，IP 不重复占用。"""
    tid = _seed_tenant("c-recon")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.25.0.0/30")
    dev = _seed_device(tid)
    l1 = lease_service.establish_session(dev, ap).lease

    barrier = threading.Barrier(4)

    def close_old():
        barrier.wait()
        return lease_service.close_session(dev, l1["lease_id"], l1["generation"])

    def reconnect():
        barrier.wait()
        return lease_service.establish_session(dev, ap)

    with ThreadPoolExecutor(max_workers=4) as ex:
        f_close = ex.submit(close_old)
        f_connects = [ex.submit(reconnect) for _ in range(3)]
        closed = f_close.result()
        connects = [f.result() for f in f_connects]

    assert closed["status"] == "closed"

    # 所有并发重连最终都指向同一条新活动租约
    final_active = _device_active_lease(dev)
    assert final_active is not None
    new_lease_id, new_generation, new_ip = final_active
    assert new_lease_id != l1["lease_id"]
    assert new_generation == l1["generation"] + 1
    for r in connects:
        assert r.lease["lease_id"] in {l1["lease_id"], new_lease_id}
        if r.lease["lease_id"] == new_lease_id:
            assert r.lease["generation"] == new_generation
    assert _active_leases(ap) == 1

    db = SessionLocal()
    assert db.get(Lease, l1["lease_id"]).status == LeaseStatus.closed
    db.close()


# ---------------------------------------------------------------- 撤销竞争

def test_revoke_concurrent_with_connects_ends_with_no_active_lease():
    tid = _seed_tenant("c-rev")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.26.0.0/28")
    dev = _seed_device(tid)

    barrier = threading.Barrier(11)

    def connect():
        barrier.wait()
        try:
            return lease_service.establish_session(dev, ap)
        except lease_service.ServiceError as e:
            return e.code

    def revoke():
        barrier.wait()
        return lease_service.revoke_device(dev)

    with ThreadPoolExecutor(max_workers=11) as ex:
        f_revoke = ex.submit(revoke)
        f_connects = [ex.submit(connect) for _ in range(10)]
        f_revoke.result()
        outcomes = [f.result() for f in f_connects]

    # 撤销完成后不存在活动租约；撤销后旧令牌也无法再连
    assert _device_active_lease(dev) is None
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.establish_session(dev, ap)
    assert ei.value.code == "device_revoked"
    # 所有并发连接要么撞上撤销（device_revoked），要么在撤销前拿到活动快照
    for o in outcomes:
        if isinstance(o, str):
            assert o == "device_revoked"
        else:
            assert isinstance(o, lease_service.ConnectResult)
    db = SessionLocal()
    n_active = db.execute(select(func.count()).select_from(Lease).where(
        Lease.device_id == dev, Lease.status == LeaseStatus.active)).scalar_one()
    db.close()
    assert n_active == 0


def test_revoke_concurrent_with_heartbeat_and_reconnect():
    tid = _seed_tenant("c-revhb")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.27.0.0/28")
    dev = _seed_device(tid)
    l1 = lease_service.establish_session(dev, ap).lease

    barrier = threading.Barrier(3)
    errors = []

    def heartbeat_old():
        barrier.wait()
        for _ in range(20):
            try:
                lease_service.heartbeat(dev, l1["lease_id"], l1["generation"])
            except lease_service.ServiceError as e:
                errors.append(e.code)
            time.sleep(0.005)

    def reconnect():
        barrier.wait()
        try:
            return lease_service.establish_session(dev, ap)
        except lease_service.ServiceError as e:
            return e.code

    def revoke():
        barrier.wait()
        time.sleep(0.02)
        lease_service.revoke_device(dev)

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_hb = ex.submit(heartbeat_old)
        f_rc = ex.submit(reconnect)
        f_rv = ex.submit(revoke)
        f_hb.result()
        rc = f_rc.result()
        f_rv.result()

    # 最终：设备已撤销，没有任何活动租约
    assert _device_active_lease(dev) is None
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.establish_session(dev, ap)
    assert ei.value.code == "device_revoked"
    # 旧租约一定处于终止态（revoked 或 closed），且迟到心跳出现过拒绝
    db = SessionLocal()
    final = db.get(Lease, l1["lease_id"])
    assert final.status != LeaseStatus.active
    db.close()
    if isinstance(rc, lease_service.ConnectResult):
        # 重连若抢在撤销前成功，新会话也必须已被撤销终止
        db = SessionLocal()
        new = db.get(Lease, rc.lease["lease_id"])
        assert new.status == LeaseStatus.revoked
        db.close()


def test_double_revoke_releases_only_once():
    tid = _seed_tenant("c-rev2")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.28.0.0/30")
    dev = _seed_device(tid)
    l1 = lease_service.establish_session(dev, ap).lease

    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda _: (barrier.wait(), lease_service.revoke_device(dev)), range(2)))

    db = SessionLocal()
    lease = db.get(Lease, l1["lease_id"])
    assert lease.status == LeaseStatus.revoked
    assert lease.termination_reason == "device_revoked"
    db.close()
    assert _terminated_event_count(l1["lease_id"]) == 1  # 同一租约只释放一次


# ---------------------------------------------------------------- 心跳超时 / 迟到心跳 / 代次

def test_expiry_late_heartbeat_and_generation(short_timeout):
    tid = _seed_tenant("e-exp")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.29.0.0/30")
    dev = _seed_device(tid)
    l1 = lease_service.establish_session(dev, ap).lease
    assert l1["generation"] == 1

    time.sleep(1.2)
    reaped = lease_service.reap_expired()
    assert any(r["lease_id"] == l1["lease_id"] and r["status"] == "expired" for r in reaped)

    # 迟到心跳（代次仍匹配，但租约已终止）：拒绝且不复活
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.heartbeat(dev, l1["lease_id"], 1)
    assert ei.value.code == "lease_terminated"

    db = SessionLocal()
    expired = db.get(Lease, l1["lease_id"])
    assert expired.status == LeaseStatus.expired
    assert expired.termination_reason == "heartbeat_timeout"
    first_ended_at = expired.ended_at
    db.close()

    # 重连使用新代次
    l2 = lease_service.establish_session(dev, ap).lease
    assert l2["lease_id"] != l1["lease_id"]
    assert l2["generation"] == 2

    # 旧代次心跳打到新租约：stale_generation，不影响新会话
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.heartbeat(dev, l2["lease_id"], 1)
    assert ei.value.code == "stale_generation"

    # 旧代次迟到心跳打到旧租约：仍然拒绝，ended_at 不变（不延长、不复活）
    with pytest.raises(lease_service.ServiceError):
        lease_service.heartbeat(dev, l1["lease_id"], 1)
    db = SessionLocal()
    assert db.get(Lease, l1["lease_id"]).ended_at == first_ended_at
    assert db.get(Lease, l2["lease_id"]).status == LeaseStatus.active
    db.close()

    # 旧代次关闭请求不能终止新会话
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.close_session(dev, l2["lease_id"], 1)
    assert ei.value.code == "stale_generation"
    assert _device_active_lease(dev)[0] == l2["lease_id"]

    # 新代次心跳正常
    lease_service.heartbeat(dev, l2["lease_id"], 2)


def test_heartbeat_prevents_expiry(short_timeout):
    tid = _seed_tenant("e-hb")
    ap = _seed_ap(tid, capacity=5)
    _seed_pool(ap, tid, "10.30.0.0/30")
    dev = _seed_device(tid)
    l1 = lease_service.establish_session(dev, ap).lease

    for _ in range(4):
        time.sleep(0.4)
        lease_service.heartbeat(dev, l1["lease_id"], 1)  # 持续心跳保活
    assert lease_service.reap_expired() == []
    assert _device_active_lease(dev) is not None


def test_heartbeat_other_device_lease_forbidden():
    tid = _seed_tenant("e-iso")
    ap = _seed_ap(tid, capacity=5)
    _seed_pool(ap, tid, "10.31.0.0/30")
    d1, d2 = _seed_device(tid), _seed_device(tid)
    l1 = lease_service.establish_session(d1, ap).lease
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.heartbeat(d2, l1["lease_id"], 1)
    assert ei.value.code == "lease_not_found"


# ---------------------------------------------------------------- 回收并发 / 重启恢复

def test_concurrent_reapers_release_each_lease_once(short_timeout):
    tid = _seed_tenant("e-reap")
    ap = _seed_ap(tid, capacity=100)
    _seed_pool(ap, tid, "10.32.0.0/24")
    devs = [_seed_device(tid) for _ in range(30)]
    lease_ids = []
    for d in devs:
        lease_ids.append(lease_service.establish_session(d, ap).lease["lease_id"])

    time.sleep(1.2)

    barrier = threading.Barrier(8)

    def reap():
        barrier.wait()
        return lease_service.reap_expired()

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(reap) for _ in range(8)]
        [f.result() for f in futures]

    assert _active_leases(ap) == 0
    for lid in lease_ids:
        assert _terminated_event_count(lid) == 1  # 每条只释放一次

    # 回收后地址全部可再分配
    dev2 = _seed_device(tid)
    again = lease_service.establish_session(dev2, ap).lease
    assert again["ip_address"] == "10.32.0.1"


def test_restart_recovery_releases_stale_keeps_fresh(short_timeout):
    """模拟服务重启：租约状态持久化在库中，启动回收只清理超时租约。

    lifespan 启动时执行的就是 lease_service.reap_expired()。
    """
    tid = _seed_tenant("e-restart")
    ap = _seed_ap(tid, capacity=10)
    _seed_pool(ap, tid, "10.33.0.0/30")
    d_stale, d_fresh = _seed_device(tid), _seed_device(tid)
    stale = lease_service.establish_session(d_stale, ap).lease
    fresh = lease_service.establish_session(d_fresh, ap).lease
    _set_last_seen(stale["lease_id"], 90)  # 模拟停机前 90s 的最后心跳

    # —— 进程“重启”后直接调用同一恢复函数 ——
    recovered = lease_service.reap_expired()
    recovered_ids = {r["lease_id"] for r in recovered}
    assert stale["lease_id"] in recovered_ids
    assert fresh["lease_id"] not in recovered_ids

    db = SessionLocal()
    assert db.get(Lease, stale["lease_id"]).status == LeaseStatus.expired
    assert db.get(Lease, fresh["lease_id"]).status == LeaseStatus.active
    db.close()

    # 被清理的地址已归还：新设备可拿到 .1
    d_new = _seed_device(tid)
    l_new = lease_service.establish_session(d_new, ap).lease
    assert l_new["ip_address"] == stale["ip_address"]


def test_connect_foreign_tenant_ap_returns_not_found():
    a, b = _seed_tenant("e-fa"), _seed_tenant("e-fb")
    ap_a = _seed_ap(a, capacity=5)
    _seed_pool(ap_a, a, "10.34.0.0/30")
    dev_b = _seed_device(b)
    with pytest.raises(lease_service.ServiceError) as ei:
        lease_service.establish_session(dev_b, ap_a)
    assert ei.value.code == "access_point_not_found"
    assert ei.value.http_status == 404
