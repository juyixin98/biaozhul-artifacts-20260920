"""FastAPI 应用：通过 HTTP 接口操作本机 Anvil 上的 MockERC20 / ShareVault。

舍入与滑点语义（与合约严格一致，全程整数）：
  - 存款份额 = floor(assets * totalSupply / totalAssets)，首存为 assets - 1000；
  - 赎回资产 = floor(shares * totalAssets / totalSupply)；
  - 滑点下界 = floor(estimate * (10000 - bps) / 10000)。
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from web3 import Web3

from . import chain as chainlib
from . import config
from . import schemas

app = FastAPI(
    title="Share Vault API（本地 Anvil）",
    version="1.0.0",
    description="份额金库舍入不变量示例后端，只连接本机测试链。",
)


def _bundle() -> chainlib.ContractBundle:
    try:
        return chainlib.load_bundle()
    except chainlib.ChainError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _account(w3: Web3, pk: str):
    try:
        return w3.eth.account.from_key(pk)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"私钥无效：{exc}") from exc


def _checksum(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return Web3.to_checksum_address(value)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"地址无效：{value}") from exc


def _send_or_400(b: chainlib.ContractBundle, acct, func):
    try:
        return chainlib._send(b.w3, acct, func)
    except chainlib.ChainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 健康检查 / 元信息
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, object]:
    try:
        w3 = chainlib.connect()
        return {
            "status": "ok",
            "chain_id": w3.eth.chain_id,
            "block": w3.eth.block_number,
        }
    except chainlib.ChainError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/vault/info", response_model=schemas.VaultInfo)
def vault_info() -> schemas.VaultInfo:
    b = _bundle()
    total_assets = b.vault.functions.totalAssets().call()
    total_shares = b.vault.functions.totalSupply().call()
    rate = (total_assets * 10**18) // total_shares if total_shares else 0
    return schemas.VaultInfo(
        vault_address=b.vault_address,
        token_address=b.token_address,
        chain_id=b.chain_id,
        total_assets=total_assets,
        total_shares=total_shares,
        exchange_rate_x1e18=rate,
        dead_shares=1000,
    )


# ---------------------------------------------------------------------------
# 预览（不发交易）
# ---------------------------------------------------------------------------

@app.get("/vault/preview/deposit", response_model=schemas.QuoteResponse)
def preview_deposit(assets: int) -> schemas.QuoteResponse:
    if assets <= 0:
        raise HTTPException(status_code=400, detail="assets 必须为正整数")
    b = _bundle()
    estimated = b.vault.functions.previewDeposit(assets).call()
    floor = chainlib.apply_slippage_floor(estimated, config.DEFAULT_SLIPPAGE_BPS)
    return schemas.QuoteResponse(
        kind="deposit",
        input_amount=assets,
        estimated_output=estimated,
        min_accepted_output=floor,
        slippage_bps=config.DEFAULT_SLIPPAGE_BPS,
    )


@app.get("/vault/preview/redeem", response_model=schemas.QuoteResponse)
def preview_redeem(shares: int) -> schemas.QuoteResponse:
    if shares <= 0:
        raise HTTPException(status_code=400, detail="shares 必须为正整数")
    b = _bundle()
    estimated = b.vault.functions.previewRedeem(shares).call()
    floor = chainlib.apply_slippage_floor(estimated, config.DEFAULT_SLIPPAGE_BPS)
    return schemas.QuoteResponse(
        kind="redeem",
        input_amount=shares,
        estimated_output=estimated,
        min_accepted_output=floor,
        slippage_bps=config.DEFAULT_SLIPPAGE_BPS,
    )


# ---------------------------------------------------------------------------
# 账户视图
# ---------------------------------------------------------------------------

@app.get("/account/{address}", response_model=schemas.AccountInfo)
def account_info(address: str) -> schemas.AccountInfo:
    addr = _checksum(address)
    b = _bundle()
    return schemas.AccountInfo(
        address=addr,
        token_balance=b.token.functions.balanceOf(addr).call(),
        share_balance=b.vault.functions.balanceOf(addr).call(),
        token_allowance=b.token.functions.allowance(addr, b.vault_address).call(),
    )


# ---------------------------------------------------------------------------
# 写操作：水龙头 / 授权 / 存款 / 赎回
# ---------------------------------------------------------------------------

@app.post("/token/mint", response_model=schemas.TxResponse)
def token_mint(req: schemas.MintRequest) -> schemas.TxResponse:
    b = _bundle()
    acct = _account(b.w3, req.private_key or config.DEFAULT_PRIVATE_KEY)
    # MockERC20.mint 只能给 msg.sender 自己铸造（faucet 语义）。
    if _checksum(req.address) != acct.address:
        raise HTTPException(
            status_code=400,
            detail=(
                "faucet 只能给交易发送者自己铸造："
                f"address={req.address} 与签名者 {acct.address} 不一致"
            ),
        )
    rcpt = _send_or_400(b, acct, b.token.functions.mint(req.amount))
    # 回显铸造金额（接收方即发送者）。
    return _tx_response(rcpt, acct.address, assets=req.amount)


@app.post("/token/approve", response_model=schemas.TxResponse)
def token_approve(req: schemas.ApproveRequest) -> schemas.TxResponse:
    b = _bundle()
    acct = _account(b.w3, req.private_key)
    rcpt = _send_or_400(b, acct, b.token.functions.approve(b.vault_address, req.amount))
    return _tx_response(rcpt, acct.address)


@app.post("/vault/deposit", response_model=schemas.TxResponse)
def vault_deposit(req: schemas.DepositRequest) -> schemas.TxResponse:
    b = _bundle()
    acct = _account(b.w3, req.private_key)
    receiver = _checksum(req.receiver) or acct.address

    estimated = b.vault.functions.previewDeposit(req.assets).call()
    if req.min_shares is not None:
        min_shares = req.min_shares
    else:
        min_shares = chainlib.apply_slippage_floor(estimated, req.slippage_bps)

    rcpt = _send_or_400(
        b, acct, b.vault.functions.deposit(req.assets, receiver, min_shares)
    )
    return _tx_response(rcpt, acct.address, shares=_shares_from_receipt(b, rcpt))


@app.post("/vault/redeem", response_model=schemas.TxResponse)
def vault_redeem(req: schemas.RedeemRequest) -> schemas.TxResponse:
    b = _bundle()
    acct = _account(b.w3, req.private_key)
    receiver = _checksum(req.receiver) or acct.address
    owner = _checksum(req.owner) or acct.address

    estimated = b.vault.functions.previewRedeem(req.shares).call()
    if req.min_assets_out is not None:
        min_out = req.min_assets_out
    else:
        min_out = chainlib.apply_slippage_floor(estimated, req.slippage_bps)

    bal_before = b.token.functions.balanceOf(receiver).call()
    rcpt = _send_or_400(
        b, acct, b.vault.functions.redeem(req.shares, receiver, owner, min_out)
    )
    bal_after = b.token.functions.balanceOf(receiver).call()
    return _tx_response(rcpt, acct.address, assets=bal_after - bal_before)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _tx_response(
    rcpt,
    from_address: str,
    shares: int | None = None,
    assets: int | None = None,
) -> schemas.TxResponse:
    tx_hash = rcpt.transactionHash
    return schemas.TxResponse(
        transaction_hash=tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash),
        block_number=rcpt.blockNumber,
        gas_used=rcpt.gasUsed,
        shares=shares,
        assets=assets,
        from_address=from_address,
    )


def _shares_from_receipt(b: chainlib.ContractBundle, rcpt) -> int | None:
    """从 Deposit(sender, owner, assets, shares) 事件解析实际铸造份额。"""
    try:
        events = b.vault.events.Deposit().process_receipt(rcpt)
    except Exception:  # noqa: BLE001
        return None
    if not events:
        return None
    return int(events[0]["args"]["shares"])
