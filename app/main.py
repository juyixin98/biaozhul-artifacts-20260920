"""FastAPI 入口：建池、快照、报价、验签。纯后端，无任何前端页面。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from . import db, service
from .clmm.crypto import using_dev_key, verify_quote
from .schemas import PoolCreate, QuoteRequest, VerifyRequest


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = db.connect()
    yield
    app.state.db.close()


app = FastAPI(
    title="集中流动性报价引擎",
    version="1.0.0",
    description="整数 tick 网格 + 自定义定点精度的离线报价服务；报价绑定池快照，不改储备。",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "hmac_dev_key_in_use": using_dev_key(),
        "warning": (
            "正在使用开发默认 HMAC 密钥，请通过 CLMM_HMAC_KEY 环境变量设置生产密钥"
            if using_dev_key()
            else None
        ),
    }


@app.post("/pools", status_code=201)
def create_pool(req: PoolCreate) -> dict:
    try:
        snap = service.create_pool(app.state.db, req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return snap


@app.get("/pools")
def list_pools() -> dict:
    return {"pool_ids": db.list_pool_ids(app.state.db)}


@app.get("/pools/{pool_id}")
def get_pool(pool_id: str) -> dict:
    row = db.get_pool_row(app.state.db, pool_id)
    if row is None:
        raise HTTPException(status_code=404, detail="池不存在")
    positions = [dict(r) for r in db.get_positions(app.state.db, pool_id)]
    snap = service.load_snapshot(app.state.db, pool_id)
    return {
        "pool_id": row["pool_id"],
        "created_at": row["created_at"],
        "token0": row["token0"],
        "token1": row["token1"],
        "fee_ppm": row["fee_ppm"],
        "sqrt_price_x96": row["sqrt_price_x96"],
        "positions": positions,
        "snapshot_hash": snap["snapshot_hash"],
    }


@app.get("/pools/{pool_id}/snapshot")
def get_snapshot(pool_id: str) -> dict:
    try:
        return service.load_snapshot(app.state.db, pool_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="池不存在")


@app.post("/quotes", status_code=201)
def create_quote(req: QuoteRequest) -> dict:
    try:
        return service.make_quote(
            app.state.db,
            pool_id=req.pool_id,
            zero_for_one=req.zero_for_one,
            amount_in=int(req.amount_in),
            limit_tick=req.limit_tick,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="池不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OverflowError as exc:
        raise HTTPException(status_code=400, detail=f"数值过大: {exc}") from exc


@app.get("/quotes/{quote_id}")
def get_quote(quote_id: str) -> dict:
    row = db.get_quote_row(app.state.db, quote_id)
    if row is None:
        raise HTTPException(status_code=404, detail="报价不存在")
    import json

    return {"payload": json.loads(row["payload_json"]), "signature": row["signature"]}


@app.post("/quotes/verify")
def verify(req: VerifyRequest) -> dict:
    ok = verify_quote(req.payload, req.signature)
    return {"valid": ok}
