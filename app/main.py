"""FastAPI HTTP interface to the on-chain fee settlement contract (local anvil)."""
from typing import Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
from web3 import Web3

from app import chain
from app.reference import SCALE, MAX_UINT256

app = FastAPI(title="Lossless Fee Settlement", version="1.0.0")

MAX_RATE = SCALE


class OpenAccountIn(BaseModel):
    account: str
    principal: Union[int, str]
    rate: Union[int, str]

    @field_validator("principal", "rate")
    @classmethod
    def _bigint(cls, v):
        return _to_int(v)


class RateIn(BaseModel):
    rate: Union[int, str]

    @field_validator("rate")
    @classmethod
    def _bigint(cls, v):
        return _to_int(v)


class PrincipalIn(BaseModel):
    principal: Union[int, str]

    @field_validator("principal")
    @classmethod
    def _bigint(cls, v):
        return _to_int(v)


class MineIn(BaseModel):
    blocks: int = 1


def _to_int(v) -> int:
    if isinstance(v, str):
        if not v.isdigit():
            raise ValueError("must be a non-negative integer (decimal)")
        return int(v)
    if isinstance(v, int) and v >= 0:
        return v
    raise ValueError("must be a non-negative integer")


def _w3_contract():
    w3 = chain.get_web3()
    return w3, chain.get_contract(w3)


def _account_or_404(contract, account: str):
    try:
        addr = Web3.to_checksum_address(account)
    except Exception:
        raise HTTPException(status_code=422, detail="invalid account address")
    t = contract.functions.getAccount(addr).call()
    if t[0] == "0x0000000000000000000000000000000000000000":
        raise HTTPException(status_code=404, detail="unknown account")
    return addr, t


def _run_tx(w3, fn, what: str):
    try:
        return chain.send_tx(w3, fn)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"{what} failed: {e}")


@app.get("/health")
def health():
    w3 = chain.get_web3()
    return {
        "status": "ok",
        "chain_id": w3.eth.chain_id,
        "block_number": w3.eth.block_number,
        "contract": chain.resolve_contract_address(),
        "signer": chain.signer_address(w3),
    }


@app.get("/scale")
def scale():
    return {"rate_scale": str(SCALE), "max_rate": str(MAX_RATE), "max_uint256": str(MAX_UINT256)}


@app.post("/accounts")
def open_account(body: OpenAccountIn):
    w3, contract = _w3_contract()
    if body.rate > MAX_RATE:
        raise HTTPException(status_code=422, detail="rate exceeds RATE_SCALE")
    if body.principal > MAX_UINT256:
        raise HTTPException(status_code=422, detail="principal exceeds uint256")
    try:
        addr = Web3.to_checksum_address(body.account)
    except Exception:
        raise HTTPException(status_code=422, detail="invalid account address")
    receipt = _run_tx(
        w3, contract.functions.openAccount(addr, body.principal, body.rate), "openAccount"
    )
    return {"tx_hash": receipt.transactionHash.hex(), "block": receipt.blockNumber}


@app.get("/accounts/{account}")
def get_account(account: str):
    _, contract = _w3_contract()
    addr, t = _account_or_404(contract, account)
    limbs, fees_int = chain.fees_limbs_of(t)
    return {
        "account": addr,
        "owner": t[0],
        "principal": str(t[1]),
        "rate": str(t[2]),
        "last_settle_block": t[3],
        "carry": str(t[4]),
        "fees_limbs": [str(x) for x in limbs],
        "fees": str(fees_int),
    }


@app.post("/accounts/{account}/settle")
def settle(account: str):
    w3, contract = _w3_contract()
    addr, _ = _account_or_404(contract, account)
    receipt = _run_tx(w3, contract.functions.settle(addr), "settle")
    return _settle_response(contract, receipt)


@app.post("/accounts/{account}/rate")
def set_rate(account: str, body: RateIn):
    w3, contract = _w3_contract()
    addr, _ = _account_or_404(contract, account)
    if body.rate > MAX_RATE:
        raise HTTPException(status_code=422, detail="rate exceeds RATE_SCALE")
    receipt = _run_tx(w3, contract.functions.setRate(addr, body.rate), "setRate")
    return _settle_response(contract, receipt)


@app.post("/accounts/{account}/principal")
def set_principal(account: str, body: PrincipalIn):
    w3, contract = _w3_contract()
    addr, _ = _account_or_404(contract, account)
    if body.principal > MAX_UINT256:
        raise HTTPException(status_code=422, detail="principal exceeds uint256")
    receipt = _run_tx(w3, contract.functions.setPrincipal(addr, body.principal), "setPrincipal")
    return _settle_response(contract, receipt)


@app.post("/debug/mine")
def debug_mine(body: MineIn):
    """Local-chain helper: mine n empty blocks (anvil evm_mine)."""
    if body.blocks < 1 or body.blocks > 10000:
        raise HTTPException(status_code=422, detail="blocks must be in [1, 10000]")
    w3 = chain.get_web3()
    chain.mine_blocks(w3, body.blocks)
    return {"mined": body.blocks, "block_number": w3.eth.block_number}


def _settle_response(contract, receipt):
    """Extract the FeesSettled event (fee delta) from a settlement receipt."""
    from web3.logs import DISCARD

    events = contract.events.FeesSettled().process_receipt(receipt, errors=DISCARD)
    out = {"tx_hash": receipt.transactionHash.hex(), "block": receipt.blockNumber}
    if events:
        args = events[0]["args"]
        limbs = [int(args[f"delta{i}"]) for i in range(4)]
        out["from_block"] = args["fromBlock"]
        out["to_block"] = args["toBlock"]
        out["fee_delta_limbs"] = [str(x) for x in limbs]
        out["fee_delta"] = str(sum(l << (128 * i) for i, l in enumerate(limbs)))
    return out
