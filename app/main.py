"""FastAPI 入口：权益惩罚证据归并后端（纯后端，无前端）。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import slashing
from .config import JUDGE_VERSION, SLASH_RATE_DEN, SLASH_RATE_NUM
from .crypto import hex_to_bytes, verify_signature, vote_signed_bytes
from .db import close_pool, get_pool, init_pool, init_schema
from .errors import SlasherError
from .models import ChainIn, PowerIn, ValidatorIn, VoteIn
from .serialize import serialize_row


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    init_schema()
    yield
    close_pool()


app = FastAPI(
    title="权益惩罚证据归并后端",
    version="1.0.0",
    description="接收带测试签名的离线投票，归并验证者双签证据并幂等处罚。",
    lifespan=lifespan,
)


@app.exception_handler(SlasherError)
async def _slasher_error_handler(request: Request, exc: SlasherError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
    )


@app.get("/health")
def health():
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            cur.fetchone()
    return {"status": "ok", "judge_version": JUDGE_VERSION}


# ----------------------------------------------------------- 链 / 验证者


@app.post("/api/v1/chains", status_code=201)
def create_chain(body: ChainIn):
    with get_pool().connection() as conn:
        row = slashing.create_chain(conn, body.chain_id, body.epoch_length)
        conn.commit()
    return serialize_row(row)


@app.post("/api/v1/chains/{chain_id}/validators", status_code=201)
def register_validator(chain_id: str, body: ValidatorIn):
    pubkey = hex_to_bytes(body.validator_pubkey, "validator_pubkey")
    with get_pool().connection() as conn:
        slashing.register_validator(conn, chain_id, pubkey, body.moniker)
        conn.commit()
    return {"chain_id": chain_id, "validator_pubkey": body.validator_pubkey,
            "registered": True}


@app.put("/api/v1/chains/{chain_id}/validators/{validator_pubkey}/power")
def set_power(chain_id: str, validator_pubkey: str, body: PowerIn):
    pubkey = hex_to_bytes(validator_pubkey, "validator_pubkey")
    with get_pool().connection() as conn:
        slashing.set_power(conn, chain_id, pubkey, body.power)
        conn.commit()
    return {"chain_id": chain_id, "validator_pubkey": validator_pubkey,
            "power": body.power}


@app.get("/api/v1/chains/{chain_id}/validators")
def list_validators(chain_id: str):
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT v.validator_pubkey, v.moniker, vp.power
                FROM validators v
                JOIN validator_power vp
                  ON vp.chain_id = v.chain_id
                 AND vp.validator_pubkey = v.validator_pubkey
                WHERE v.chain_id = %s
                ORDER BY v.validator_pubkey
                """,
                (chain_id,),
            )
            rows = [serialize_row(r) for r in cur.fetchall()]
    return {"chain_id": chain_id, "validators": rows}


# ----------------------------------------------------------- epoch 冻结


@app.post("/api/v1/chains/{chain_id}/epochs/{epoch}/freeze", status_code=201)
def freeze_epoch(chain_id: str, epoch: int):
    if epoch < 0:
        raise SlasherError("epoch 不能为负", code="bad_epoch")
    pool = get_pool()
    with pool.connection() as conn:
        snap = slashing.freeze_epoch(conn, chain_id, epoch)
        # 冻结后立即尝试补偿：此前因快照缺失而挂起的证据。
        recovered = slashing.recover_pending(conn)
        conn.commit()
    return {"snapshot": snap, "recovery": recovered}


@app.get("/api/v1/chains/{chain_id}/epochs/{epoch}/snapshot")
def get_snapshot(chain_id: str, epoch: int):
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM stake_snapshots WHERE chain_id = %s AND epoch = %s",
                (chain_id, epoch),
            )
            snap = cur.fetchone()
            if snap is None:
                raise SlasherError("快照不存在", code="snapshot_missing", status_code=404)
            cur.execute(
                """
                SELECT validator_pubkey, power
                FROM stake_snapshot_entries
                WHERE chain_id = %s AND epoch = %s
                ORDER BY validator_pubkey
                """,
                (chain_id, epoch),
            )
            entries = [serialize_row(r) for r in cur.fetchall()]
    return {"snapshot": serialize_row(snap), "entries": entries}


# ----------------------------------------------------------- 投票归并


@app.post("/api/v1/chains/{chain_id}/votes")
async def submit_vote(chain_id: str, body: VoteIn, request: Request):
    if body.chain_id != chain_id:
        raise SlasherError(
            "URL 链 ID 与投票载荷中的 chain_id 不一致，拒绝跨链混用",
            code="chain_mismatch",
        )

    pubkey = hex_to_bytes(body.validator_pubkey, "validator_pubkey")
    block_hash = hex_to_bytes(body.block_hash, "block_hash")
    signature = hex_to_bytes(body.signature, "signature")

    # 1) 真实 Ed25519 验签（链 ID 绑定在被签字节中）。
    signed = vote_signed_bytes(
        chain_id=body.chain_id,
        validator_pubkey=pubkey,
        round=body.round,
        block_hash=block_hash,
    )
    if not verify_signature(pubkey, signature, signed):
        raise SlasherError("签名验证失败", code="bad_signature", status_code=422)

    # 保存投票原文：按实际收到的 JSON 文本解析回对象存证。
    raw = await request.json()

    # 2) 归并事务（证据与投票先落盘）。
    pool = get_pool()
    with pool.connection() as conn:
        outcome = slashing.ingest_vote(
            conn,
            chain_id=body.chain_id,
            pubkey=pubkey,
            round=body.round,
            block_hash=block_hash,
            signature=signature,
            raw=raw,
        )
        conn.commit()

    result = outcome["result"]
    response: dict = {
        "chain_id": body.chain_id,
        "validator_pubkey": body.validator_pubkey,
        "round": body.round,
        "block_hash": body.block_hash,
        "result": result,
    }

    # 3) 新证据：独立事务处罚。证据已提交，崩溃也不会重复处罚。
    if result == "new_evidence":
        eid_hex = outcome["evidence_id"]
        # 故障注入点（仅测试环境）：证据已提交、惩罚未应用时硬退出。
        slashing.crash_if_injected()
        eid = bytes.fromhex(eid_hex)
        with pool.connection() as conn:
            penalty_outcome = slashing.apply_penalty(conn, evidence_id=eid)
            conn.commit()
        response["evidence_id"] = eid_hex
        response["penalty"] = penalty_outcome
        return response

    if result in ("already_evidence",):
        response["evidence_id"] = outcome["evidence_id"]
        response["evidence_status"] = outcome["evidence_status"]
    return response


# ----------------------------------------------------------- 证据 / 惩罚查询


@app.get("/api/v1/evidences")
def list_evidences(
    chain_id: str | None = None,
    validator_pubkey: str | None = None,
    status: str | None = Query(None, pattern="^(pending|punished)$"),
    limit: int = Query(100, ge=1, le=500),
):
    where, params = [], []
    if chain_id:
        where.append("chain_id = %s")
        params.append(chain_id)
    if validator_pubkey:
        where.append("validator_pubkey = %s")
        params.append(hex_to_bytes(validator_pubkey, "validator_pubkey"))
    if status:
        where.append("status = %s")
        params.append(status)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT evidence_id, chain_id, validator_pubkey, round, epoch,
                       status, judge_version, first_seen_at, raw_evidence
                FROM evidences {clause}
                ORDER BY first_seen_at DESC LIMIT %s
                """,
                params,
            )
            rows = [serialize_row(r) for r in cur.fetchall()]
    return {"evidences": rows, "count": len(rows)}


@app.get("/api/v1/evidences/{evidence_id}")
def get_evidence(evidence_id: str):
    eid = hex_to_bytes(evidence_id, "evidence_id")
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM evidences WHERE evidence_id = %s", (eid,))
            ev = cur.fetchone()
            if ev is None:
                raise SlasherError("证据不存在", code="evidence_missing", status_code=404)
            cur.execute(
                "SELECT * FROM penalties WHERE evidence_id = %s", (eid,)
            )
            pen = cur.fetchone()
            cur.execute(
                """
                SELECT chain_id, round, block_hash, signature, content_hash,
                       received_at, raw_vote
                FROM votes WHERE evidence_id = %s
                ORDER BY received_at, signature
                """,
                (eid,),
            )
            votes = [serialize_row(r) for r in cur.fetchall()]
    return {
        "evidence": serialize_row(ev),
        "penalty": serialize_row(pen) if pen else None,
        "votes": votes,
    }


# ----------------------------------------------------------- 管理


@app.post("/api/v1/admin/recover")
def admin_recover():
    """崩溃恢复入口：补罚所有 pending 证据（快照未冻结的继续挂起）。幂等。"""
    with get_pool().connection() as conn:
        out = slashing.recover_pending(conn)
        conn.commit()
    return out


@app.get("/api/v1/params")
def params():
    return {
        "judge_version": JUDGE_VERSION,
        "slash_rate": f"{SLASH_RATE_NUM}/{SLASH_RATE_DEN}",
        "signature_scheme": "Ed25519",
    }
