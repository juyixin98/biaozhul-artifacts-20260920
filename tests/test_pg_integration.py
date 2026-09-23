"""PostgreSQL 集成测试。

仅在设置 DATABASE_URL（形如 postgresql+asyncpg://user:pass@host/db）时运行。
本地可用 Docker 一键起库：
    docker run -d --rm --name lreplay-pg -e POSTGRES_PASSWORD=postgres \
        -p 5432:5432 postgres:16-alpine
    DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/postgres \
        pytest -m integration
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.repository import PgRepository
from app.service import ReplayService

pytestmark = pytest.mark.integration

DB_URL = os.environ.get("DATABASE_URL", "")


@pytest.fixture
async def pg_repo():
    if not DB_URL:
        pytest.skip("未设置 DATABASE_URL")
    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        repo = PgRepository(session)
        await repo.init_schema()
        await repo.reset()
        yield repo
        await repo.reset()
    await engine.dispose()


async def test_pg_persistence_and_versioned_reports(pg_repo, feeder_kp, server_kp):
    from tests.conftest import make_settings, sign_event

    settings = make_settings(database_url=DB_URL)
    svc = ReplayService(
        pg_repo, settings,
        verify_key=feeder_kp.signing_key.verify_key,
        signing_key=server_kp.signing_key,
    )
    sk = feeder_kp.signing_key

    e1 = [
        sign_event(sk, "r1", 0, "rate_schedule", {
            "rate_per_second": "0.000000001", "tier1_m1": "1000000",
            "tier2_m2": "0", "liq_bonus_num": 11, "liq_bonus_den": 10}),
        sign_event(sk, "o1", 0, "open_position", {"position_id": "p"}),
    ]
    await svc.ingest_batch(e1)

    # 新会话/新服务实例读取历史（验证持久化而非进程内状态）
    engine = create_async_engine(DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as session:
        svc2 = ReplayService(
            PgRepository(session), settings,
            verify_key=feeder_kp.signing_key.verify_key,
            signing_key=server_kp.signing_key,
        )
        ids = {e["event_id"] for e in await svc2.events()}
        assert ids == {"r1", "o1"}

        # 迟到事件产生 v2，v1 保留
        late = [sign_event(sk, "pETH", 0, "price_update",
                           {"asset": "ETH", "price": "1000"})]
        res = await svc2.ingest_batch(late)
        assert res["late_detected"] is False  # 同时间不算迟到
        assert res["replay_version"] == 2
        assert (await svc2.report(1)) is not None
        assert (await svc2.report(2))["prev_digest"] is not None
    await engine.dispose()
