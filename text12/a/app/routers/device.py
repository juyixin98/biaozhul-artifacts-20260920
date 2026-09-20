"""设备数据面 API（模拟控制平面，不建立真实 VPN / 不触碰主机路由）。

所有接口用 X-Device-Token 认证；租约操作都强制校验“租约属于该设备 + 代次匹配”。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import lease_service
from app.config import settings
from app.db import get_db
from app.deps import require_device
from app.models import Device, Lease, LeaseStatus
from app.schemas import CloseRequest, ConnectRequest, HeartbeatRequest, LeaseOut

router = APIRouter(prefix="/device", tags=["device"])


def _lease_response(snapshot: dict, reused: bool = False) -> LeaseOut:
    return LeaseOut(**snapshot, reused=reused)


@router.get("/me")
def device_me(device: Device = Depends(require_device)) -> dict:
    return {
        "device_id": str(device.id),
        "tenant_id": str(device.tenant_id),
        "name": device.name,
        "status": device.status.value,
        "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
        "heartbeat_timeout_seconds": settings.heartbeat_timeout_seconds,
    }


@router.post("/sessions/connect", response_model=LeaseOut, status_code=201)
def connect(
    payload: ConnectRequest,
    db: Session = Depends(get_db),
    device: Device = Depends(require_device),
) -> LeaseOut:
    """建立（或幂等复用）会话。

    - 原子：容量检查 + 唯一 IP 分配 + 设备绑定在同一个 SERIALIZABLE 事务；
    - 同一设备的并发/重复连接请求收敛到同一条活动租约（reused=true）；
    - 撤销后的令牌在认证层即被拒绝。
    """
    result = lease_service.establish_session(device.id, payload.access_point_id)
    return _lease_response(result.lease, reused=result.reused)


@router.post("/leases/{lease_id}/heartbeat", response_model=LeaseOut)
def post_heartbeat(
    lease_id: str,
    payload: HeartbeatRequest,
    db: Session = Depends(get_db),
    device: Device = Depends(require_device),
) -> LeaseOut:
    """心跳上报。旧代次或已终止租约的迟到心跳一律 409，且不会复活/延长租约。"""
    lease = lease_service.heartbeat(device.id, lease_id, payload.generation)
    return _lease_response(lease)


@router.post("/leases/{lease_id}/close", response_model=LeaseOut)
def post_close(
    lease_id: str,
    payload: CloseRequest,
    db: Session = Depends(get_db),
    device: Device = Depends(require_device),
) -> LeaseOut:
    """主动关闭会话。代次不匹配拒绝；同代次重复关闭幂等，租约只释放一次。"""
    snapshot = lease_service.close_session(device.id, lease_id, payload.generation)
    return _lease_response(snapshot)


@router.get("/sessions/current", response_model=LeaseOut | None)
def current_session(
    db: Session = Depends(get_db),
    device: Device = Depends(require_device),
) -> LeaseOut | None:
    lease = db.execute(
        select(Lease)
        .where(Lease.device_id == device.id, Lease.status == LeaseStatus.active)
    ).scalar_one_or_none()
    return _lease_response(lease_service.lease_out(lease)) if lease else None
