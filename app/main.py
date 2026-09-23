"""FastAPI 应用入口。纯后端 JSON API，无前端页面。"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import schemas, service
from .service import FinalityService, VoteRejected
from .storage import Database

DEFAULT_DB = os.environ.get("FINALITY_DB", "finality.db")


def create_app(db_path: str = DEFAULT_DB) -> FastAPI:
    db = Database(db_path)
    checked = service.reconcile(db)  # 启动即重放核对；不一致直接抛错拒绝启动
    svc = FinalityService(db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        db.close()

    app = FastAPI(
        title="终局性分歧检测器",
        version="1.0.0",
        description="按 epoch 固定验证者集合的加权投票终局检测；严格 >2/3 整数判定。",
        lifespan=lifespan,
    )
    app.state.db = db
    app.state.svc = svc
    app.state.reconciled_epochs = checked

    @app.exception_handler(VoteRejected)
    async def vote_rejected_handler(request: Request, exc: VoteRejected):
        return JSONResponse(
            status_code=exc.status,
            content={"accepted": False, "stored": False, "reason": exc.reason},
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "reconciled_epochs": app.state.reconciled_epochs}

    @app.post("/epochs", status_code=201)
    def create_epoch(body: schemas.CreateEpochRequest):
        return svc.create_epoch(
            body.epoch, [v.model_dump() for v in body.validators]
        )

    @app.get("/epochs", response_model=list[schemas.EpochSummary])
    def list_epochs():
        return [
            {
                "epoch": r["epoch"],
                "state": r["state"],
                "total_weight": r["total_weight"],
                "finalized_block": r["finalized_block"],
            }
            for r in db.list_epochs()
        ]

    @app.get("/epochs/{epoch}/status", response_model=schemas.EpochStatus)
    def epoch_status(epoch: int):
        return service.build_status(db, epoch)

    @app.post("/votes", response_model=schemas.AcceptedOut)
    def submit_vote(body: schemas.VoteIn):
        return svc.submit_vote(body.model_dump())

    @app.get("/checkpoints", response_model=list[schemas.CheckpointOut])
    def list_checkpoints():
        rows = db.conn.execute(
            "SELECT epoch, block_hash, weight, required_weight, total_weight "
            "FROM checkpoints ORDER BY epoch"
        ).fetchall()
        return [dict(r) for r in rows]

    @app.get("/epochs/{epoch}/evidence")
    def epoch_evidence(epoch: int):
        """双投与终局冲突的完整可验证证据（含原始签名投票）。"""
        if db.get_epoch(epoch) is None:
            raise VoteRejected(f"未知 epoch：{epoch}", 404)
        equivs = []
        for row in db.get_equivocations(epoch):
            vote_ids = json.loads(row["vote_ids"])
            votes = db.conn.execute(
                "SELECT id, validator, block_hash, signature FROM votes "
                f"WHERE id IN ({','.join('?' * len(vote_ids))}) ORDER BY id",
                vote_ids,
            ).fetchall()
            equivs.append(
                {
                    "validator_id": row["validator"],
                    "weight": row["weight"],
                    "block_hashes": json.loads(row["block_hashes"]),
                    "votes": [dict(v) for v in votes],
                }
            )
        conflict_row = db.get_conflict(epoch)
        return {
            "epoch": epoch,
            "equivocations": equivs,
            "conflict": (
                json.loads(conflict_row["evidence"]) if conflict_row else None
            ),
        }

    return app


app = create_app()
