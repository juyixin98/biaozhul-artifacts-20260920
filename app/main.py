"""FastAPI 应用：追加审计日志、签名检查点、区间导出、验证。"""

from __future__ import annotations

import base64
import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .canonical import canonical_json
from .chain import GENESIS_PREV_HASH, build_checkpoint, build_entry
from .keys import ensure_keypair, public_key_pem
from .storage import LogStore
from .verify import verify_export

LOG_ID = "audit-log"


class AppendRequest(BaseModel):
    actor: str = Field(..., examples=["alice"])
    action: str = Field(..., examples=["invoice.approve"])
    payload: Any = Field(default=None, examples=[{"invoice_id": "INV-1024"}])


class AppendResponse(BaseModel):
    entry: dict
    entry_hash: str


class CheckpointResponse(BaseModel):
    checkpoint: dict
    signature: str  # base64url(Ed25519 signature)


class ExportResponse(BaseModel):
    log_id: str
    from_seq: int
    to_seq: int
    entries: list[dict]
    checkpoint: Optional[dict] = None
    checkpoint_signature: Optional[str] = None


class VerifyRequest(BaseModel):
    entries: list[dict] = Field(
        ..., description='[{"entry": {...}, "entry_hash": "sha256:..."}]'
    )
    public_key_pem: str = Field(..., description="验证者持有的公钥（信任锚）")
    checkpoint: Optional[dict] = None
    checkpoint_signature: Optional[str] = None
    trusted_checkpoint: Optional[dict] = None
    trusted_signature: Optional[str] = None


def create_app(
    data_dir: str | None = None,
    key_dir: str | None = None,
) -> FastAPI:
    data_dir = data_dir or os.environ.get("AUDIT_DATA_DIR", "data")
    key_dir = key_dir or os.environ.get("AUDIT_KEY_DIR", "keys")

    store = LogStore(data_dir)
    private_key, key_id = ensure_keypair(key_dir)

    app = FastAPI(title="Tamper-Evident Audit Log", version="1.0.0")
    app.state.store = store
    app.state.key_id = key_id

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "log_id": LOG_ID, "key_id": key_id}

    @app.get("/public_key")
    def get_public_key() -> dict:
        """返回签名公钥。注意：生产环境中验证者应通过带外渠道获取并固定公钥，
        本接口仅为演示方便。"""
        return {"key_id": key_id, "public_key_pem": public_key_pem(private_key)}

    @app.post("/entries", response_model=AppendResponse, status_code=201)
    def append_entry(req: AppendRequest) -> dict:
        head_seq, head_hash = store.head()
        entry, entry_hash = build_entry(
            seq=head_seq + 1,
            prev_hash=head_hash or GENESIS_PREV_HASH,
            actor=req.actor,
            action=req.action,
            payload=req.payload,
        )
        store.append_entry(entry, entry_hash)
        return {"entry": entry, "entry_hash": entry_hash}

    @app.get("/entries")
    def list_entries(
        from_seq: int = Query(1, ge=1),
        to_seq: Optional[int] = Query(None, ge=1),
    ) -> dict:
        entries = store.read_entries()
        sliced = [
            e
            for e in entries
            if e["entry"]["seq"] >= from_seq
            and (to_seq is None or e["entry"]["seq"] <= to_seq)
        ]
        return {"from_seq": from_seq, "to_seq": to_seq, "entries": sliced}

    @app.post("/checkpoints", response_model=CheckpointResponse, status_code=201)
    def create_checkpoint() -> dict:
        head_seq, head_hash = store.head()
        if head_seq == 0:
            raise HTTPException(status_code=409, detail="空日志无法创建检查点")
        checkpoint = build_checkpoint(
            log_id=LOG_ID, upto_seq=head_seq, head_hash=head_hash, key_id=key_id
        )
        signature = base64.urlsafe_b64encode(
            private_key.sign(canonical_json(checkpoint))
        ).decode("ascii").rstrip("=")
        store.append_checkpoint(checkpoint, signature)
        return {"checkpoint": checkpoint, "signature": signature}

    @app.get("/checkpoints")
    def list_checkpoints() -> dict:
        return {"checkpoints": store.read_checkpoints()}

    @app.get("/export", response_model=ExportResponse)
    def export_range(
        from_seq: int = Query(1, ge=1),
        to_seq: Optional[int] = Query(None, ge=1),
    ) -> dict:
        """区间导出：记录 + 最新签名检查点。验证者另需自行持有公钥与可信锚。"""
        entries = store.read_entries()
        sliced = [
            e
            for e in entries
            if e["entry"]["seq"] >= from_seq
            and (to_seq is None or e["entry"]["seq"] <= to_seq)
        ]
        latest = store.latest_checkpoint()
        return {
            "log_id": LOG_ID,
            "from_seq": from_seq,
            "to_seq": to_seq if to_seq is not None else (sliced[-1]["entry"]["seq"] if sliced else 0),
            "entries": sliced,
            "checkpoint": latest["checkpoint"] if latest else None,
            "checkpoint_signature": latest["signature"] if latest else None,
        }

    @app.post("/verify")
    def verify(req: VerifyRequest) -> dict:
        """验证一份导出。信任锚（公钥、可信检查点）由验证者在请求中自带，
        服务端不替验证者存储锚。"""
        return verify_export(
            entries=req.entries,
            public_key_pem=req.public_key_pem,
            checkpoint=req.checkpoint,
            checkpoint_signature=req.checkpoint_signature,
            trusted_checkpoint=req.trusted_checkpoint,
            trusted_signature=req.trusted_signature,
        )

    return app


app = create_app()
