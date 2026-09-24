"""FastAPI 服务：通过 HTTP 接口操作本地 Anvil 上的多签延时执行器。

接口:
  GET  /health                        链与合约连通性
  GET  /state                         签名人/阈值/延时/nonce
  POST /operations                    提案：绑定 target/data/value/nonce/validUntil
  POST /actions/increment             便捷动作：对 Counter.increment() 提案
  POST /signers/change                便捷动作：多签提案变更签名人/阈值
  POST /operations/{op_hash}/sign     指定签名人测试密钥离线 EIP-712 签名
  POST /operations/{op_hash}/approve  提交已收集签名上链（去重在合约层完成）
  GET  /operations/{op_hash}          查询操作状态
  GET  /operations                    列出本地提案
  POST /operations/{op_hash}/execute  到期执行（失败按 retryCooldown 重试）
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from web3 import Web3

from .chain import WalletService, load_abi

ROOT = Path(__file__).resolve().parent.parent
DEPLOYMENT_PATH = os.environ.get("DEPLOYMENT_PATH", str(ROOT / "deployments.json"))


# ----------------------------------------------------------------------
# 请求模型（必须定义在模块顶层，FastAPI 才能正确解析请求体注解）
# ----------------------------------------------------------------------

class OperationIn(BaseModel):
    target: str
    data: str = Field("0x", description="十六进制 calldata")
    value: int = 0
    nonce: int | None = None
    valid_until: int = Field(0, alias="validUntil", description="0 表示不限期")

    model_config = {"populate_by_name": True}


class SignIn(BaseModel):
    signer: str
    nonce: int
    valid_until: int = Field(0, alias="validUntil")
    model_config = {"populate_by_name": True}


class ApproveIn(BaseModel):
    nonce: int
    valid_until: int = Field(0, alias="validUntil")
    signers: list[str] | None = None  # 指定使用哪些已收集签名；None=全部
    model_config = {"populate_by_name": True}


class ExecuteIn(BaseModel):
    nonce: int
    valid_until: int = Field(0, alias="validUntil")
    model_config = {"populate_by_name": True}


class IncrementIn(BaseModel):
    valid_until: int = Field(0, alias="validUntil")
    model_config = {"populate_by_name": True}


class ChangeSignersIn(BaseModel):
    signers: list[str]
    threshold: int
    valid_until: int = Field(0, alias="validUntil")
    model_config = {"populate_by_name": True}


# ----------------------------------------------------------------------
# 进程内提案/签名存储（演示用，重启清空；真相始终以链上为准）
# ----------------------------------------------------------------------

class Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ops: dict[str, dict] = {}

    def add_op(self, op: dict) -> None:
        with self._lock:
            self.ops[op["op_hash"]] = op

    def get_op(self, op_hash: str) -> dict:
        with self._lock:
            if op_hash not in self.ops:
                raise KeyError(op_hash)
            return self.ops[op_hash]

    def list_ops(self) -> list[dict]:
        with self._lock:
            return list(self.ops.values())

    def add_signature(self, op_hash: str, signer: str, sig: bytes) -> None:
        with self._lock:
            self.ops[op_hash].setdefault("signatures", {})[signer.lower()] = "0x" + sig.hex()


store = Store()


def _jsonable(row: dict) -> dict:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (bytes, bytearray)):
            out[k] = "0x" + v.hex()
        elif isinstance(v, (int, bool, str)) or v is None:
            out[k] = v
        else:
            out[k] = str(v)
    return out


def _decode_hex(data: str) -> bytes:
    try:
        return bytes.fromhex(data.removeprefix("0x"))
    except ValueError as exc:
        raise HTTPException(400, "data 必须是十六进制") from exc


def create_app(service: WalletService | None = None) -> FastAPI:
    app = FastAPI(title="MultiSig Timelock Executor API", version="1.0.0")
    svc = service  # 测试可注入；否则首次请求时从部署文件加载

    def get_svc() -> WalletService:
        nonlocal svc
        if svc is None:
            svc = WalletService.from_deployment(DEPLOYMENT_PATH)
        return svc

    def _op_record(s: WalletService, target: str, data: bytes, value: int,
                   nonce: int, valid_until: int) -> dict:
        op_hash = s.hash_operation(target, data, value, nonce, valid_until)
        rec = {
            "op_hash": "0x" + op_hash.hex(),
            "target": target,
            "data": "0x" + data.hex(),
            "value": str(value),
            "nonce": nonce,
            "valid_until": valid_until,
            "signatures": {},
        }
        store.add_op(rec)
        return rec

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    @app.get("/health")
    def health() -> dict:
        s = get_svc()
        return {
            "connected": s.w3.is_connected(),
            "chain_id": s.chain_id,
            "block_number": s.w3.eth.block_number,
            "wallet": s.address,
        }

    @app.get("/state")
    def state() -> dict:
        s = get_svc()
        return {
            "wallet": s.address,
            "counter": s.counter_address,
            "chain_id": s.chain_id,
            "signers": s.signers,
            "threshold": s.threshold,
            "onchain_threshold": s.contract.functions.threshold().call(),
            "delay_seconds": s.delay,
            "retry_cooldown_seconds": s.cooldown,
            "now": s.now(),
        }

    @app.post("/operations")
    def propose(body: OperationIn) -> dict:
        s = get_svc()
        target = Web3.to_checksum_address(body.target)
        data = _decode_hex(body.data)
        nonce = body.nonce if body.nonce is not None else s.next_nonce
        if body.nonce is None:
            s.next_nonce += 1
        return _op_record(s, target, data, body.value, nonce, body.valid_until)

    @app.post("/actions/increment")
    def action_increment(body: IncrementIn) -> dict:
        s = get_svc()
        counter = s.w3.eth.contract(address=s.counter_address, abi=load_abi("Counter"))
        data = counter.encode_abi("increment", [])
        nonce = s.next_nonce
        s.next_nonce += 1
        return _op_record(s, s.counter_address, bytes.fromhex(data.removeprefix("0x")),
                          0, nonce, body.valid_until)

    @app.post("/signers/change")
    def action_change_signers(body: ChangeSignersIn) -> dict:
        s = get_svc()
        if not (1 <= body.threshold <= len(body.signers)):
            raise HTTPException(400, "阈值必须在 1..签名人数 之间")
        addrs = [Web3.to_checksum_address(a) for a in body.signers]
        if len(set(addrs)) != len(addrs):
            raise HTTPException(400, "签名人不可重复")
        data = s.contract.encode_abi("configureSigners", [addrs, body.threshold])
        nonce = s.next_nonce
        s.next_nonce += 1
        return _op_record(s, s.address, bytes.fromhex(data.removeprefix("0x")),
                          0, nonce, body.valid_until)

    @app.post("/operations/{op_hash}/sign")
    def sign_op(op_hash: str, body: SignIn) -> dict:
        s = get_svc()
        try:
            rec = store.get_op(op_hash.lower())
        except KeyError:
            raise HTTPException(404, "未知操作（请先 POST /operations 提案）")
        signer = Web3.to_checksum_address(body.signer)
        if not s.is_signer(signer):
            raise HTTPException(403, f"{signer} 不是当前签名人")
        if signer.lower() not in s.signer_keys:
            raise HTTPException(403, "服务未持有该签名人的测试密钥")
        if (rec["nonce"], rec["valid_until"]) != (body.nonce, body.valid_until):
            raise HTTPException(400, "nonce/validUntil 与提案不一致")
        data = bytes.fromhex(rec["data"].removeprefix("0x"))
        sig = s.sign_operation(signer, rec["target"], data, int(rec["value"]),
                               rec["nonce"], rec["valid_until"])
        store.add_signature(rec["op_hash"], signer, sig)
        return {"op_hash": rec["op_hash"], "signed_by": signer,
                "signature": "0x" + sig.hex(),
                "collected": list(rec["signatures"].keys())}

    @app.post("/operations/{op_hash}/approve")
    def approve_op(op_hash: str, body: ApproveIn) -> dict:
        s = get_svc()
        try:
            rec = store.get_op(op_hash.lower())
        except KeyError:
            raise HTTPException(404, "未知操作")
        if (rec["nonce"], rec["valid_until"]) != (body.nonce, body.valid_until):
            raise HTTPException(400, "nonce/validUntil 与提案不一致")
        chosen = set(body.signers) if body.signers else set(rec["signatures"].keys())
        sig_map = rec["signatures"]
        missing = [a for a in chosen if a.lower() not in sig_map]
        if missing:
            raise HTTPException(400, f"缺少签名: {missing}")
        # 故意打乱顺序，证明合约层乱序安全
        sigs = [bytes.fromhex(sig_map[a.lower()].removeprefix("0x"))
                for a in sorted(chosen, key=str.lower, reverse=True)]
        data = bytes.fromhex(rec["data"].removeprefix("0x"))
        func = s.contract.functions.approve(
            rec["target"], data, int(rec["value"]), rec["nonce"],
            rec["valid_until"], sigs,
        )
        result = s.send_function(s.signers[0], func)
        onchain = s.get_operation(rec["op_hash"])
        return {"tx": result, "onchain": _jsonable(onchain),
                "submitted_signatures": len(sigs)}

    @app.get("/operations/{op_hash}")
    def get_op(op_hash: str) -> dict:
        s = get_svc()
        try:
            rec = store.get_op(op_hash.lower())
        except KeyError:
            rec = None
        try:
            onchain = s.get_operation(op_hash)
        except Exception:
            raise HTTPException(400, "op_hash 非法")
        return {"proposal": rec, "onchain": _jsonable(onchain)}

    @app.get("/operations")
    def list_operations() -> dict:
        return {"operations": store.list_ops()}

    @app.post("/operations/{op_hash}/execute")
    def execute_op(op_hash: str, body: ExecuteIn) -> dict:
        s = get_svc()
        try:
            rec = store.get_op(op_hash.lower())
        except KeyError:
            raise HTTPException(404, "未知操作")
        if (rec["nonce"], rec["valid_until"]) != (body.nonce, body.valid_until):
            raise HTTPException(400, "nonce/validUntil 与提案不一致")
        data = bytes.fromhex(rec["data"].removeprefix("0x"))
        func = s.contract.functions.execute(
            rec["target"], data, 0, rec["nonce"], rec["valid_until"],
        )
        result = s.send_function(s.signers[0], func)
        receipt = s.w3.eth.wait_for_transaction_receipt(result["tx_hash"])
        events = s.decode_logs(receipt)
        onchain = s.get_operation(rec["op_hash"])

        outcome = "unknown"
        next_try_after = None
        for ev in events:
            if ev["event"] == "Executed":
                outcome = "executed"
            elif ev["event"] == "ExecutionFailed":
                outcome = "failed_retryable"
                next_try_after = ev["args"].get("nextTryAfter")
        return JSONResponse(status_code=200, content={
            "tx": result, "events": events, "outcome": outcome,
            "next_try_after": next_try_after,
            "onchain": _jsonable(onchain), "counter": s.counter_value(),
        })

    return app


app = create_app()
