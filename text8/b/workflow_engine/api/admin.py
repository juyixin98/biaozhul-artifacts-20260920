"""内部管理接口：手动触发超时扫描（测试/演示用）、种子演示数据、健康检查。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from workflow_engine.database import SessionLocal, get_session
from workflow_engine.demo import seed_demo
from workflow_engine.services.templates import get_instance_definition
from workflow_engine.timeouts import run_sweep

router = APIRouter(tags=["admin"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/admin/sweep")
def trigger_sweep() -> dict:
    """同步执行一轮超时升级扫描。"""
    session = SessionLocal()
    try:
        return run_sweep(session, get_definition=get_instance_definition)
    finally:
        session.close()


@router.post("/demo/seed")
def demo_seed(session: Session = Depends(get_session)) -> dict:
    with session.begin():
        return seed_demo(session)
