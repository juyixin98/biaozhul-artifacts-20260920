from collections.abc import Callable

from sqlalchemy.orm import Session

from workflow_engine.errors import ReplayControl
from workflow_engine.idempotency import claim_or_replay, store_response


def run_idempotent(session: Session, request_id: str, build_payload: Callable[[], dict]) -> tuple[dict, bool]:
    """在单个事务内完成：占位 → 业务执行 → 回填响应。

    返回 (payload, replayed)。重复请求回放首次成功响应；
    业务失败时整个事务回滚（包含占位行），不写历史、不占用 request_id。
    """
    try:
        with session.begin():
            claim_or_replay(session, request_id)
            payload = build_payload()
            store_response(session, request_id, payload)
        return payload, False
    except ReplayControl as replay:
        session.rollback()
        return replay.payload, True
