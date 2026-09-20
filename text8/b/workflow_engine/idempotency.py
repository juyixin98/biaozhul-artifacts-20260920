"""幂等请求支持。

约定：
- 每个写请求带 ``request_id``；同一 request_id 的重复请求直接返回**首次成功的响应**；
- 业务冲突 / 权限错误 / 版本不符等 4xx **不会**写入台账（随事务整体回滚），
  因此调用方修正请求后可以复用同一个 request_id 重试；
- 台账插入与状态/待办/审计在同一事务提交，保证"只有一次有效转换"。
"""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from workflow_engine.errors import ReplayControl
from workflow_engine.models import RequestLedger


def claim_or_replay(session: Session, request_id: str) -> None:
    """在业务事务内调用。

    - request_id 未出现过：尝试占位（SAVEPOINT），成功后继续执行业务；
    - 已存在：抛出 ``ReplayControl``，由最外层捕获并回放历史响应。
    """
    existing = session.scalar(
        select(RequestLedger).where(RequestLedger.request_id == request_id)
    )
    if existing is not None:
        raise ReplayControl(existing.response_payload)

    savepoint = session.begin_nested()
    try:
        session.add(RequestLedger(request_id=request_id, response_payload={}))
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = session.scalar(
            select(RequestLedger).where(RequestLedger.request_id == request_id)
        )
        raise ReplayControl(existing.response_payload if existing else {})
    # 保留 savepoint 不释放：业务失败时随外层回滚一起消失；
    # 成功提交时由外层事务最终提交。


def store_response(session: Session, request_id: str, payload: dict) -> None:
    """业务成功后，把完整响应回填到本事务早先占位的台账行。"""
    row = session.scalar(select(RequestLedger).where(RequestLedger.request_id == request_id))
    if row is not None:
        row.response_payload = payload
