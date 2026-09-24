"""FastAPI 应用：部署合约、查询分配/证明、提交单笔与批量领取。

启动时：
- 若环境变量 CONTRACT_ADDRESS 已给出 → 连接已部署合约（必须与 allocations.json 匹配）；
- 否则等待 POST /deploy（或用 scripts/deploy.py），未部署时领取接口返回 409。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from web3 import Web3
from web3.exceptions import ContractLogicError

from . import chain as chain_mod
from .allocations import Allocation, load_allocations
from .config import Settings
from .merkle import get_proof


class ClaimRequest(BaseModel):
    index: int = Field(ge=0)


class BatchClaimRequest(BaseModel):
    indexes: list[int] = Field(min_length=1)


class DeployRequest(BaseModel):
    # 可选：向合约注入的资金（wei），默认所有分配数量之和
    fund_wei: int | None = Field(default=None, ge=0)


@dataclass
class AppState:
    settings: Settings
    w3: Web3
    allocations: list[Allocation]
    contract: Any = None  # web3 Contract | None
    root_hex: str | None = None

    def by_index(self, index: int) -> Allocation:
        for a in self.allocations:
            if a.index == index:
                return a
        raise KeyError(index)

    def proof_for(self, a: Allocation) -> list[bytes]:
        if self.contract is None:
            raise HTTPException(status_code=409, detail="contract not deployed")
        leaves = chain_mod.leaves_for(
            self.allocations, self.w3.eth.chain_id, self.contract.address
        )
        return get_proof(leaves, a.index)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    w3 = chain_mod.connect(settings.rpc_url)
    allocations = load_allocations(settings.allocations_path)

    state = AppState(settings=settings, w3=w3, allocations=allocations)

    contract = chain_mod.load_contract(w3, settings.contract_address, settings.artifact_path)
    if contract is not None:
        state.contract = contract
        state.root_hex = "0x" + contract.functions.merkleRoot().call().hex()

    app = FastAPI(
        title="Merkle 批量领取后端",
        version="1.0.0",
        description="叶子绑定 chainId + 合约地址 + index + account + amount；位图防重；批量原子。",
    )
    app.state.depot = state

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "rpc_connected": w3.is_connected(),
            "chain_id": w3.eth.chain_id,
            "deployed": state.contract is not None,
            "contract_address": state.contract.address if state.contract else None,
            "allocations": len(state.allocations),
        }

    @app.get("/allocations")
    def list_allocations() -> list[dict[str, Any]]:
        return [a.model_dump() for a in state.allocations]

    @app.get("/allocations/{index}/proof")
    def allocation_proof(index: int) -> dict[str, Any]:
        try:
            a = state.by_index(index)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"index {index} not found")
        claimed = False
        if state.contract is not None:
            claimed = bool(state.contract.functions.isClaimed(index).call())
        proof = state.proof_for(a)
        return {
            "index": a.index,
            "account": a.account,
            "amount_wei": a.amount_wei,
            "proof": ["0x" + p.hex() for p in proof],
            "claimed": claimed,
            "root": state.root_hex,
        }

    @app.post("/deploy")
    def deploy(req: DeployRequest) -> dict[str, Any]:
        if state.contract is not None:
            raise HTTPException(status_code=409, detail="contract already deployed")
        fund = (
            req.fund_wei
            if req.fund_wei is not None
            else sum(a.amount_wei for a in state.allocations)
        )
        contract, address, root, nonce = chain_mod.deploy_claim_contract(
            w3,
            state.settings.deployer_private_key,
            state.allocations,
            state.settings.artifact_path,
            fund,
        )
        state.contract = contract
        state.root_hex = "0x" + root.hex()
        return {
            "contract_address": address,
            "merkle_root": state.root_hex,
            "deploy_nonce": nonce,
            "funded_wei": str(fund),
            "chain_id": w3.eth.chain_id,
        }

    @app.post("/claim")
    def claim(req: ClaimRequest) -> dict[str, Any]:
        if state.contract is None:
            raise HTTPException(status_code=409, detail="contract not deployed")
        try:
            a = state.by_index(req.index)
        except KeyError:
            raise HTTPException(status_code=404, detail=f"index {req.index} not found")
        proof = state.proof_for(a)
        try:
            receipt = chain_mod.send_claim(
                w3,
                state.contract,
                state.settings.claimer_private_key,
                a.index,
                a.account,
                a.amount_wei,
                proof,
            )
        except (ContractLogicError, chain_mod.TransactionReverted) as exc:
            # 重复领取 / 证明失效等链上 revert
            raise HTTPException(status_code=422, detail=f"claim reverted: {exc}") from exc
        return _receipt_summary(receipt, [[a.index, a.account, str(a.amount_wei)]])

    @app.post("/claim/batch")
    def claim_batch(req: BatchClaimRequest) -> dict[str, Any]:
        if state.contract is None:
            raise HTTPException(status_code=409, detail="contract not deployed")
        indexes = req.indexes
        accounts: list[str] = []
        amounts: list[int] = []
        proofs: list[list[bytes]] = []
        try:
            for idx in indexes:
                a = state.by_index(idx)
                accounts.append(a.account)
                amounts.append(a.amount_wei)
                proofs.append(state.proof_for(a))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"index {exc.args[0]} not found")

        try:
            receipt = chain_mod.send_claim_batch(
                w3,
                state.contract,
                state.settings.claimer_private_key,
                indexes,
                accounts,
                amounts,
                proofs,
            )
        except chain_mod.BatchRejected as exc:
            # estimateGas 判定整批必 revert（单项证明无效/重复/转账失败）。
            # 交易从未发出，链上状态零变化，天然满足原子性。
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "batch rejected atomically before submission",
                    "reason": str(exc),
                    "indexes": indexes,
                    "tx_sent": False,
                },
            )
        except ContractLogicError as exc:
            raise HTTPException(status_code=422, detail=f"batch reverted: {exc}") from exc

        claimed = [
            [i, acc, str(amt)] for i, acc, amt in zip(indexes, accounts, amounts)
        ]
        return _receipt_summary(receipt, claimed)

    def _receipt_summary(receipt: Any, claimed: list[list[Any]]) -> dict[str, Any]:
        return {
            "transaction_hash": receipt["transactionHash"].hex(),
            "block_number": receipt["blockNumber"],
            "gas_used": receipt["gasUsed"],
            "status": receipt["status"],
            "claimed": claimed,
        }

    return app
