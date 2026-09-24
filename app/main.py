"""FastAPI 服务：订单签名、结算、取消、状态查询，全部打到本地 Anvil。"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from eth_account import Account
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError

from . import errors as revert_errors
from .config import Settings
from .signing import ORDER_KEYS, order_to_tuple, sign_order

_ROLE_KEYS = {"deployer": "deployer_pk", "maker": "maker_pk", "taker": "taker_pk"}


class AppState:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url, request_kwargs={"timeout": 20}))
        self.settlement = self._load_contract("BoundedSettlement", settings.settlement)
        self.token_a = self._load_contract("MockERC20", settings.token_a)
        self.token_b = self._load_contract("MockERC20", settings.token_b)

    def _load_contract(self, name: str, address: str) -> Contract:
        artifact = self.settings.out_dir / f"{name}.sol" / f"{name}.json"
        abi = json.loads(artifact.read_text())["abi"]
        return self.w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)

    def pk_for(self, role_or_pk: str) -> str:
        if role_or_pk in _ROLE_KEYS:
            return getattr(self.settings, _ROLE_KEYS[role_or_pk])
        pk = role_or_pk if role_or_pk.startswith("0x") else "0x" + role_or_pk
        if len(pk) != 66:
            raise HTTPException(status_code=400, detail=f"非法私钥/角色: {role_or_pk}")
        return pk

    def resolve_token(self, token: str) -> str:
        aliases = {
            "A": self.settings.token_a,
            "B": self.settings.token_b,
            "TKA": self.settings.token_a,
            "TKB": self.settings.token_b,
        }
        return Web3.to_checksum_address(aliases.get(token.upper(), token))


def _send_tx(state: AppState, contract: Contract, func: Any, private_key: str) -> dict[str, Any]:
    """构建并发送交易；合约回滚时把自定义错误解码成 409。"""
    w3 = state.w3
    acct = Account.from_key(private_key)

    base = {"from": acct.address, "chainId": w3.eth.chain_id}
    try:
        gas = func.estimate_gas(base)
    except ContractLogicError as exc:
        raise _revert_http(exc) from exc
    except Exception as exc:  # noqa: BLE001 - 个别节点把回滚包成别的异常
        decoded = revert_errors.decode_revert(revert_errors.extract_revert_data(exc))
        if decoded:
            raise HTTPException(status_code=409, detail=f"交易将回滚: {decoded}") from exc
        raise HTTPException(status_code=400, detail=f"gas 估算失败: {exc}") from exc

    tx_params = base | {"gas": int(gas * 1.25)}
    latest = w3.eth.get_block("latest")
    if "baseFeePerGas" in latest:
        priority = w3.eth.max_priority_fee
        tx_params["maxFeePerGas"] = latest["baseFeePerGas"] * 2 + priority
        tx_params["maxPriorityFeePerGas"] = priority
    else:
        tx_params["gasPrice"] = w3.eth.gas_price
    tx_params["nonce"] = w3.eth.get_transaction_count(acct.address)

    # 关键：由合约函数构建出含 to/data 的交易
    tx = func.build_transaction(tx_params)
    signed = w3.eth.account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
    return {
        "txHash": tx_hash.hex(),
        "status": receipt.status,
        "blockNumber": receipt.blockNumber,
        "gasUsed": receipt.gasUsed,
    }


def _revert_http(exc: BaseException) -> HTTPException:
    decoded = revert_errors.decode_revert(revert_errors.extract_revert_data(exc)) or str(exc)
    return HTTPException(status_code=409, detail=f"合约回滚: {decoded}")


# ---- 请求/响应模型 ----


class SignRequest(BaseModel):
    signer: str = Field("maker", description="签名角色: maker/taker/deployer，或 0x 私钥")
    sell_token: str = Field("A", description="A/B、代币符号或地址")
    buy_token: str = Field("B")
    sell_amount: int = Field(..., gt=0, description="maker 卖出额度（最小单位整数）")
    buy_amount: int = Field(..., gt=0, description="完全成交时 taker 支付量")
    fee_cap: int = Field(0, ge=0, description="订单累计费用上限（buyToken）")
    nonce: int = Field(..., ge=0)
    expiry: int | None = Field(None, description="unix 秒；不传 = 当前时间 +3600")


class FillRequest(BaseModel):
    order: dict
    signature: str
    sell_fill_amount: int = Field(..., gt=0)
    fee_amount: int = Field(0, ge=0)
    taker: str = Field("taker", description="taker 角色或私钥")


class CancelRequest(BaseModel):
    nonce: int
    maker: str = Field("maker")


# ---- 应用 ----


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        app.state.svc = AppState(settings)
        yield

    app = FastAPI(title="Bounded Settlement API", version="1.0.0", lifespan=lifespan)

    def state() -> AppState:
        return app.state.svc

    def _normalize_order(raw: dict) -> dict:
        missing = [k for k in ORDER_KEYS if k not in raw]
        if missing:
            raise HTTPException(status_code=400, detail=f"订单缺少字段: {missing}")
        s = state()
        return {
            "maker": Web3.to_checksum_address(raw["maker"]),
            "sellToken": s.resolve_token(str(raw["sellToken"])),
            "buyToken": s.resolve_token(str(raw["buyToken"])),
            "sellAmount": int(raw["sellAmount"]),
            "buyAmount": int(raw["buyAmount"]),
            "feeCap": int(raw["feeCap"]),
            "nonce": int(raw["nonce"]),
            "expiry": int(raw["expiry"]),
        }

    @app.get("/health")
    def health() -> dict:
        s = state()
        return {
            "rpc_url": s.settings.rpc_url,
            "chain_id": s.w3.eth.chain_id,
            "block_number": s.w3.eth.block_number,
            "contract": s.settings.settlement,
            "tokens": {"A": s.settings.token_a, "B": s.settings.token_b},
        }

    @app.post("/orders/sign")
    def sign(r: SignRequest) -> dict:
        s = state()
        pk = s.pk_for(r.signer)
        maker_addr = Account.from_key(pk).address
        order = {
            "maker": maker_addr,
            "sellToken": s.resolve_token(r.sell_token),
            "buyToken": s.resolve_token(r.buy_token),
            "sellAmount": r.sell_amount,
            "buyAmount": r.buy_amount,
            "feeCap": r.fee_cap,
            "nonce": r.nonce,
            # 默认基于链上时间（若节点时间被 warp 也能保持语义一致）
            "expiry": r.expiry if r.expiry is not None else s.w3.eth.get_block("latest")["timestamp"] + 3600,
        }
        signature = sign_order(pk, s.settings.chain_id, s.settings.settlement, order)
        # 让链上 hashOrder 与本地 EIP-712 摘要做一次对拍
        order_hash = s.settlement.functions.hashOrder(order_to_tuple(order)).call()
        return {"order": order, "signature": signature, "order_hash": order_hash.hex()}

    @app.post("/settlements")
    def settle(r: FillRequest) -> JSONResponse:
        s = state()
        order = _normalize_order(r.order)
        func = s.settlement.functions.fillOrder(
            order_to_tuple(order), r.sell_fill_amount, r.fee_amount, r.signature
        )
        tx = _send_tx(s, s.settlement, func, s.pk_for(r.taker))

        # 解析 OrderFilled 事件（只看本合约发出的日志，避免 ERC20 Transfer 的 ABI 不匹配警告）
        filled: dict[str, Any] = {}
        receipt = s.w3.eth.wait_for_transaction_receipt(tx["txHash"])
        try:
            own_logs = [
                log for log in receipt["logs"]
                if log["address"].lower() == s.settings.settlement.lower()
            ]
            for log in own_logs:
                decoded = s.settlement.events.OrderFilled().process_log(log)
                args = decoded["args"]
                filled = {
                    "orderHash": args["orderHash"].hex() if isinstance(args["orderHash"], bytes) else args["orderHash"],
                    "sellFilled": args["sellFilled"],
                    "buyPaid": args["buyPaid"],
                    "feePaid": args["feePaid"],
                    "totalSellFilled": args["totalSellFilled"],
                }
        except Exception:  # noqa: BLE001
            pass
        return JSONResponse({"tx": tx, "order": order, "filled": filled})

    @app.post("/cancellations")
    def cancel(r: CancelRequest) -> dict:
        s = state()
        pk = s.pk_for(r.maker)
        func = s.settlement.functions.cancelNonce(r.nonce)
        tx = _send_tx(s, s.settlement, func, pk)
        return {"tx": tx, "cancelled_nonce": r.nonce}

    @app.get("/orders/{order_hash}")
    def order_state(order_hash: str) -> dict:
        s = state()
        h = bytes.fromhex(order_hash[2:]) if order_hash.startswith("0x") else bytes.fromhex(order_hash)
        sell, buy, fee = s.settlement.functions.filledSellAmount(h).call(), \
            s.settlement.functions.filledBuyAmount(h).call(), \
            s.settlement.functions.filledFeeAmount(h).call()
        return {"filledSellAmount": sell, "filledBuyAmount": buy, "filledFeeAmount": fee}

    @app.get("/balances")
    def balances(address: str) -> dict:
        s = state()
        addr = Web3.to_checksum_address(address)
        return {
            "address": addr,
            "tokenA": s.token_a.functions.balanceOf(addr).call(),
            "tokenB": s.token_b.functions.balanceOf(addr).call(),
        }

    @app.exception_handler(ContractLogicError)
    async def contract_logic_handler(request: Any, exc: ContractLogicError) -> JSONResponse:  # pragma: no cover
        return JSONResponse(status_code=409, content={"detail": f"合约回滚: {exc}"})

    return app


app = create_app()
