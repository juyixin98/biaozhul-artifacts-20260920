"""租约/会话核心领域服务。

并发正确性依赖三道防线：
1. 每个写操作在独立的 PostgreSQL SERIALIZABLE 事务中完成；
2. 事务内对 device / access_point 行加 FOR UPDATE 行锁串行化；
3. 数据库部分唯一索引兜底：同一设备唯一活动租约、同一接入点内同一 IP 唯一占用。

代次（generation）：设备每建立一次新会话，generation 在历史最大值上 +1。
心跳与关闭必须携带正确 generation；旧会话的迟到心跳永远命中不上新代次，
任何非 active 租约都不会被心跳复活或延长。

所有函数自管会话（不从请求中接收 Session），以保证隔离级别在事务首条语句前设置，
也便于在服务重启后独立执行恢复清理。返回值均为脱离会话的普通 dict/对象。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.addressing import choose_address
from app.config import settings
from app.db import SessionLocal
from app.models import (
    AccessPoint,
    AddressPool,
    Device,
    DeviceStatus,
    Lease,
    LeaseEvent,
    LeaseStatus,
    utcnow,
)

logger = logging.getLogger("cloudgate.lease")

_RETRYABLE_PG_CODES = {
    "40001",  # serialization_failure
    "40P01",  # deadlock_detected
    "23505",  # unique_violation（兜底唯一索引冲突，重算后重试/收敛幂等）
}

MAX_RETRIES = 10


class ServiceError(Exception):
    def __init__(self, code: str, message: str, http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


@dataclass
class ConnectResult:
    lease: dict
    reused: bool


@contextmanager
def serializable_tx() -> Iterator[Session]:
    """全新会话 + SERIALIZABLE 事务；退出时自动回滚未提交内容。"""
    db = SessionLocal()
    # 必须在事务首条语句前设置；psycopg2 方言会在 BEGIN 后立即发出
    # SET TRANSACTION ISOLATION LEVEL SERIALIZABLE。
    db.connection(execution_options={"isolation_level": "SERIALIZABLE"})
    try:
        yield db
    finally:
        db.close()


def _is_retryable(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return sqlstate in _RETRYABLE_PG_CODES


def _add_event(
    db: Session,
    lease: Lease | None,
    event_type: str,
    reason: str | None = None,
    detail: dict | None = None,
    tenant_id=None,
) -> None:
    db.add(
        LeaseEvent(
            lease_id=lease.id if lease else None,
            tenant_id=lease.tenant_id if lease else tenant_id,
            event_type=event_type,
            reason=reason,
            detail=detail,
        )
    )


def _terminate_locked(db: Session, lease: Lease, status: LeaseStatus, reason: str, now) -> bool:
    """终止一条已被 FOR UPDATE 锁定的租约。

    仅做 active -> 终止态的转换，保证同一租约在撤销/过期/关闭并发时只释放一次。
    """
    if lease.status != LeaseStatus.active:
        return False
    lease.status = status
    lease.ended_at = now
    lease.termination_reason = reason
    _add_event(db, lease, "terminated", reason)
    return True


def lease_out(lease: Lease) -> dict:
    return {
        "lease_id": str(lease.id),
        "generation": lease.generation,
        "tenant_id": str(lease.tenant_id),
        "access_point_id": str(lease.access_point_id),
        "device_id": str(lease.device_id),
        "ip_address": str(lease.ip_address),
        "status": lease.status.value,
        "connected_at": lease.connected_at,
        "last_seen_at": lease.last_seen_at,
        "ended_at": lease.ended_at,
        "termination_reason": lease.termination_reason,
    }


def _as_uuid(value, name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError) as exc:
        raise ServiceError("invalid_id", f"非法的 {name} 标识", 400) from exc


# ---------------------------------------------------------------- 建立会话

def establish_session(device_id, access_point_id) -> ConnectResult:
    """原子地检查接入点容量、分配唯一 IP 并绑定设备。

    重复连接（设备已有活动会话）幂等返回原租约；并发冲突/唯一索引冲突时重试。
    """
    device_id = _as_uuid(device_id, "device")
    access_point_id = _as_uuid(access_point_id, "access_point")
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with serializable_tx() as db:
                return _establish_once(db, device_id, access_point_id)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable(exc):
                raise
            last_exc = exc
            logger.warning("establish_session 冲突重试 #%s device=%s (%s)",
                           attempt, device_id, type(exc).__name__)
    raise last_exc  # type: ignore[misc]


def _establish_once(db: Session, device_id, access_point_id) -> ConnectResult:
    now = utcnow()

    # 1) 锁定设备行 —— 串行化同设备的并发连接 / 撤销
    dev = db.get(Device, device_id, with_for_update=True)
    if dev is None or dev.status == DeviceStatus.revoked:
        raise ServiceError("device_revoked", "设备已被撤销，令牌已失效", 403)

    # 2) 幂等：设备已有活动会话则直接返回（重复/并发连接请求收敛于此）
    existing = db.execute(
        select(Lease)
        .where(Lease.device_id == dev.id, Lease.status == LeaseStatus.active)
        .with_for_update()
    ).scalar_one_or_none()
    if existing is not None:
        snapshot = lease_out(existing)
        db.commit()
        return ConnectResult(lease=snapshot, reused=True)

    # 3) 锁定接入点行（同时做租户隔离校验），串行化同 AP 的并发接入
    ap = db.get(AccessPoint, access_point_id, with_for_update=True)
    if ap is None or ap.tenant_id != dev.tenant_id:
        # 对其他租户资源统一 404，不泄露其存在性
        raise ServiceError("access_point_not_found", "接入点不存在", 404)

    # 4) 容量检查
    active_count = db.execute(
        select(func.count())
        .select_from(Lease)
        .where(Lease.access_point_id == ap.id, Lease.status == LeaseStatus.active)
    ).scalar_one()
    if active_count >= ap.capacity:
        raise ServiceError("ap_full", f"接入点容量已满（{active_count}/{ap.capacity}）", 409)

    # 5) 锁定 AP 上现存活动租约并收集已占用 IP
    occupied_rows = db.execute(
        select(Lease.ip_address)
        .where(Lease.access_point_id == ap.id, Lease.status == LeaseStatus.active)
        .with_for_update()
    ).all()
    occupied = {str(ip) for (ip,) in occupied_rows}

    # 6) 在地址池可分配集合中（已排除网络/广播/保留地址）选一个空闲 IP
    pools_rows = db.execute(
        select(AddressPool.id, AddressPool.cidr,
               AddressPool.reserved_first, AddressPool.reserved_last)
        .where(AddressPool.access_point_id == ap.id)
        .order_by(AddressPool.created_at, AddressPool.id)
    ).all()
    chosen = choose_address(
        [(str(pid), cidr, rf, rl) for pid, cidr, rf, rl in pools_rows],
        occupied,
    )
    if chosen is None:
        raise ServiceError(
            "address_pool_exhausted", "接入点地址池已无可用地址（已排除网络/广播/保留地址）", 409
        )

    # 7) 代次 = 该设备历史最大代次 + 1（重连即新代次）
    max_gen = db.execute(
        select(func.coalesce(func.max(Lease.generation), 0)).where(Lease.device_id == dev.id)
    ).scalar_one()

    lease = Lease(
        tenant_id=dev.tenant_id,
        access_point_id=ap.id,
        device_id=dev.id,
        pool_id=chosen.pool_id,
        generation=int(max_gen) + 1,
        ip_address=chosen.ip,
        status=LeaseStatus.active,
        token_hash=dev.token_hash,
        connected_at=now,
        last_seen_at=now,
    )
    db.add(lease)
    db.flush()  # 触发部分唯一索引冲突 -> 重试（换地址 / 收敛幂等 / 重新评估容量）
    _add_event(db, lease, "created", detail={"ip": chosen.ip, "ap_id": str(ap.id)})
    snapshot = lease_out(lease)
    db.commit()
    return ConnectResult(lease=snapshot, reused=False)


# ---------------------------------------------------------------- 心跳 / 关闭

def heartbeat(device_id, lease_id, generation: int) -> dict:
    """处理设备心跳。

    - 仅当租约属于该设备、status=active、generation 完全匹配时才刷新 last_seen_at；
    - 旧代次迟到心跳：拒绝，不复活任何租约；
    - 已终止租约的心跳：拒绝，不复活、不延长新会话。
    """
    device_id = _as_uuid(device_id, "device")
    lease_id = _as_uuid(lease_id, "lease")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with serializable_tx() as db:
                lease = db.execute(
                    select(Lease).where(Lease.id == lease_id).with_for_update()
                ).scalar_one_or_none()

                if lease is None or lease.device_id != device_id:
                    raise ServiceError("lease_not_found", "租约不存在", 404)

                if lease.generation != generation:
                    _add_event(db, lease, "heartbeat_rejected", "stale_generation",
                               detail={"sent_generation": generation, "current_generation": lease.generation})
                    db.commit()
                    raise ServiceError(
                        "stale_generation",
                        f"心跳代次 {generation} 已失效（当前代次 {lease.generation}）",
                        409,
                    )

                if lease.status != LeaseStatus.active:
                    _add_event(db, lease, "heartbeat_rejected", lease.termination_reason or "terminated",
                               detail={"sent_generation": generation})
                    db.commit()
                    raise ServiceError(
                        "lease_terminated",
                        f"会话已终止（{lease.termination_reason}），迟到心跳不能复活",
                        409,
                    )

                lease.last_seen_at = utcnow()
                db.commit()
                return lease_out(lease)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001  与回收/撤销并发时可能序列化失败
            if not _is_retryable(exc):
                raise
            logger.warning("heartbeat 冲突重试 #%s", attempt)
    raise ServiceError("busy", "心跳冲突过多，请重试", 503)


def close_session(device_id, lease_id, generation: int) -> dict:
    """设备主动关闭会话。

    - 代次不匹配一律拒绝（旧代次不能关闭新会话）；
    - 同代次重复关闭幂等成功，但租约只释放一次。
    """
    device_id = _as_uuid(device_id, "device")
    lease_id = _as_uuid(lease_id, "lease")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with serializable_tx() as db:
                lease = db.execute(
                    select(Lease).where(Lease.id == lease_id).with_for_update()
                ).scalar_one_or_none()
                if lease is None or lease.device_id != device_id:
                    raise ServiceError("lease_not_found", "租约不存在", 404)
                if lease.generation != generation:
                    _add_event(db, lease, "close_rejected", "stale_generation",
                               detail={"sent_generation": generation})
                    db.commit()
                    raise ServiceError("stale_generation", "关闭请求的代次不匹配", 409)

                _terminate_locked(db, lease, LeaseStatus.closed, "closed", utcnow())
                db.commit()
                return lease_out(lease)
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable(exc):
                raise
            logger.warning("close_session 冲突重试 #%s", attempt)
    raise ServiceError("busy", "关闭会话冲突过多，请重试", 503)


# ---------------------------------------------------------------- 撤销 / 过期回收

def revoke_device(device_id) -> dict | None:
    """撤销设备：立即终止其活动会话并释放地址；撤销后旧令牌无法再连接。"""
    device_id = _as_uuid(device_id, "device")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with serializable_tx() as db:
                dev = db.get(Device, device_id, with_for_update=True)
                if dev is None:
                    raise ServiceError("device_not_found", "设备不存在", 404)

                if dev.status != DeviceStatus.revoked:
                    dev.status = DeviceStatus.revoked
                    dev.revoked_at = utcnow()

                lease = db.execute(
                    select(Lease)
                    .where(Lease.device_id == dev.id, Lease.status == LeaseStatus.active)
                    .with_for_update()
                ).scalar_one_or_none()
                if lease is not None:
                    _terminate_locked(db, lease, LeaseStatus.revoked, "device_revoked", utcnow())
                    snapshot = lease_out(lease)
                else:
                    snapshot = None
                db.commit()
                return snapshot
        except ServiceError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable(exc):
                raise
            logger.warning("revoke_device 冲突重试 #%s", attempt)
    raise ServiceError("busy", "撤销冲突过多，请重试", 503)


def reap_expired(*, limit: int = 500) -> list[dict]:
    """回收心跳超时的活动租约。

    - cutoff：last_seen_at 早于 now - heartbeat_timeout（默认 10 分钟）；
    - FOR UPDATE SKIP LOCKED：多个回收实例/撤销并发时互不阻塞，
      每条租约只会被一个事务终止；
    - 服务重启后租约状态全部持久化在库中，启动时执行一次即完成恢复清理；
    - 条件终止（仅 active 可转换）保证同一租约只释放一次。
    """
    cutoff = utcnow() - timedelta(seconds=settings.heartbeat_timeout_seconds)
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with serializable_tx() as db:
                stale = db.execute(
                    select(Lease)
                    .where(Lease.status == LeaseStatus.active, Lease.last_seen_at < cutoff)
                    .order_by(Lease.last_seen_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                ).scalars().all()

                now = utcnow()
                for lease in stale:
                    if _terminate_locked(db, lease, LeaseStatus.expired, "heartbeat_timeout", now):
                        logger.info("租约过期释放 lease=%s device=%s ip=%s",
                                    lease.id, lease.device_id, lease.ip_address)
                snapshots = [lease_out(l) for l in stale]
                db.commit()
                return snapshots
        except Exception as exc:  # noqa: BLE001
            if not _is_retryable(exc):
                raise
            logger.warning("reap_expired 冲突重试 #%s", attempt)
    return []
