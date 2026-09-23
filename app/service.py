"""服务层：池快照组装、纯引擎调用、快照绑定与签名。"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import db
from .clmm.crypto import canonical_json, sha256_hex, sign_quote, snapshot_hash
from .clmm.engine import Pool, Position, quote_swap


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def build_snapshot(
    pool_id: str,
    token0: str,
    token1: str,
    fee_ppm: int,
    sqrt_price_x96: int,
    positions: list[Position],
) -> dict:
    snap = {
        "schema_version": 1,
        "pool_id": pool_id,
        "token0": token0,
        "token1": token1,
        "fee_ppm": fee_ppm,
        "sqrt_price_x96": str(sqrt_price_x96),
        "positions": [
            {
                "lower_tick": p.lower_tick,
                "upper_tick": p.upper_tick,
                "liquidity": str(p.liquidity),
            }
            for p in positions
        ],
    }
    snap["snapshot_hash"] = snapshot_hash(snap)
    return snap


def load_pool(conn: sqlite3.Connection, pool_id: str) -> Pool:
    row = db.get_pool_row(conn, pool_id)
    if row is None:
        raise KeyError(pool_id)
    positions = tuple(
        Position(
            lower_tick=r["lower_tick"],
            upper_tick=r["upper_tick"],
            liquidity=int(r["liquidity"]),
        )
        for r in db.get_positions(conn, pool_id)
    )
    return Pool(
        pool_id=row["pool_id"],
        token0=row["token0"],
        token1=row["token1"],
        fee_ppm=row["fee_ppm"],
        sqrt_price_x96=int(row["sqrt_price_x96"]),
        positions=positions,
    )


def load_snapshot(conn: sqlite3.Connection, pool_id: str) -> dict:
    pool = load_pool(conn, pool_id)
    return build_snapshot(
        pool.pool_id,
        pool.token0,
        pool.token1,
        pool.fee_ppm,
        pool.sqrt_price_x96,
        list(pool.positions),
    )


def create_pool(conn: sqlite3.Connection, req: dict) -> dict:
    positions = [
        Position(
            lower_tick=p["lower_tick"],
            upper_tick=p["upper_tick"],
            liquidity=int(p["liquidity"]),
        )
        for p in req["positions"]
    ]
    if db.get_pool_row(conn, req["pool_id"]) is not None:
        raise ValueError(f"pool_id 已存在: {req['pool_id']}")

    snap = build_snapshot(
        req["pool_id"],
        req["token0"],
        req["token1"],
        req["fee_ppm"],
        int(req["sqrt_price_x96"]),
        positions,
    )
    snap["created_at"] = _utcnow()
    # 哈希只覆盖不变语义字段；created_at 不参与快照身份
    db.insert_pool(conn, snap)
    return snap


def make_quote(
    conn: sqlite3.Connection,
    pool_id: str,
    zero_for_one: bool,
    amount_in: int,
    limit_tick: Optional[int] = None,
) -> dict:
    pool = load_pool(conn, pool_id)
    snap = load_snapshot(conn, pool_id)

    result = quote_swap(pool, zero_for_one, amount_in, limit_tick)
    payload = {
        "schema_version": 1,
        "quote_id": "q_" + uuid.uuid4().hex,
        "created_at": _utcnow(),
        "pool_id": pool_id,
        "snapshot_hash": snap["snapshot_hash"],
        "zero_for_one": zero_for_one,
        "amount_in": str(amount_in),
        "limit_tick": limit_tick,
        "result": result.to_dict(),
    }
    signature = sign_quote(payload)
    request_hash = sha256_hex(
        canonical_json(
            {
                "pool_id": pool_id,
                "zero_for_one": zero_for_one,
                "amount_in": str(amount_in),
                "limit_tick": limit_tick,
                "snapshot_hash": snap["snapshot_hash"],
            }
        )
    )
    db.insert_quote(conn, payload, signature, request_hash)
    return {"payload": payload, "signature": signature}
