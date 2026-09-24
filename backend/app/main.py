"""Share vault HTTP API (FastAPI + web3.py) backed by a local Anvil chain.

The server holds a local test key (Anvil account #0 by default) and acts as
the depositor/redeemer. Amounts are wei integers encoded as strings.
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from web3.logs import DISCARD

from . import chain
from .schemas import (
    AccountState,
    DepositRequest,
    DepositResponse,
    FaucetRequest,
    PreviewDeposit,
    PreviewRedeem,
    RedeemRequest,
    RedeemResponse,
    TxResponse,
    VaultState,
)

app = FastAPI(title="Share Vault API", version="1.0.0")

ROUNDING_DOC = {
    "deposit": "shares minted = floor(assets * (supply + 1000) / (assetsHeld + 1)) — rounds DOWN",
    "redeem": "assets paid = floor(shares * (assetsHeld + 1) / (supply + 1000)) — rounds DOWN",
    "empty_vault": "rate defined via virtual offsets: 1 wei deposits mint 1000 shares, round-trip exactly",
    "donation_defense": "virtual share offset 1000 makes the first-depositor inflation attack cost ~1000x the gain",
    "slippage": "pass min_shares_out / min_assets_out (or max_slippage_bps) to bound execution",
}


def _err(exc: chain.ChainError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/health")
def health() -> dict:
    try:
        w3 = chain.get_web3()
    except chain.ChainError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "status": "ok",
        "rpc": chain.get_settings().rpc_url,
        "chain_id": w3.eth.chain_id,
        "block_number": w3.eth.block_number,
        "server_account": chain.server_address(),
    }


@app.get("/vault", response_model=VaultState)
def vault_state() -> VaultState:
    try:
        vault, asset = chain.get_contracts()
        return VaultState(
            vault=vault.address,
            asset=asset.address,
            total_assets=str(chain.call(vault.functions.totalAssets)),
            total_supply=str(chain.call(vault.functions.totalSupply)),
            assets_per_share=str(chain.call(vault.functions.assetsPerShare)),
            virtual_share_offset=str(chain.call(vault.functions.VIRTUAL_SHARE_OFFSET)),
            rounding=ROUNDING_DOC,
        )
    except chain.ChainError as exc:
        raise _err(exc) from exc


@app.get("/vault/preview/deposit", response_model=PreviewDeposit)
def preview_deposit(assets: str) -> PreviewDeposit:
    if not assets.isdigit():
        raise HTTPException(status_code=422, detail="assets must be an integer string")
    try:
        vault, _ = chain.get_contracts()
        shares = chain.call(vault.functions.convertToShares, int(assets))
        return PreviewDeposit(assets=assets, shares=str(shares))
    except chain.ChainError as exc:
        raise _err(exc) from exc


@app.get("/vault/preview/redeem", response_model=PreviewRedeem)
def preview_redeem(shares: str) -> PreviewRedeem:
    if not shares.isdigit():
        raise HTTPException(status_code=422, detail="shares must be an integer string")
    try:
        vault, _ = chain.get_contracts()
        assets = chain.call(vault.functions.convertToAssets, int(shares))
        return PreviewRedeem(shares=shares, assets=str(assets))
    except chain.ChainError as exc:
        raise _err(exc) from exc


def _min_from_slippage(preview: int, explicit: str | None, bps: int | None) -> int:
    if explicit is not None:
        return int(explicit)
    if bps is not None:
        return preview * (10_000 - bps) // 10_000
    return 0


@app.post("/vault/deposit", response_model=DepositResponse)
def deposit(req: DepositRequest) -> DepositResponse:
    try:
        vault, asset = chain.get_contracts()
        me = chain.server_address()
        assets = int(req.assets)

        quoted = chain.call(vault.functions.convertToShares, assets)
        min_shares = _min_from_slippage(quoted, req.min_shares_out, req.max_slippage_bps)

        # Ensure allowance, then deposit. The vault pulls the tokens.
        allowance = chain.call(asset.functions.allowance, me, vault.address)
        if allowance < assets:
            chain.transact(asset.functions.approve, vault.address, 2**256 - 1)
        receipt = chain.transact(vault.functions.deposit, assets, me, min_shares)

        vault_contract, _ = chain.get_contracts()
        events = vault_contract.events.Deposit().process_receipt(receipt["receipt"], errors=DISCARD)
        shares_minted = events[0]["args"]["shares"] if events else quoted
        return DepositResponse(
            tx_hash=receipt["tx_hash"],
            block_number=receipt["block_number"],
            gas_used=receipt["gas_used"],
            assets_in=str(assets),
            shares_minted=str(shares_minted),
            receiver=me,
        )
    except chain.ChainError as exc:
        raise _err(exc) from exc


@app.post("/vault/redeem", response_model=RedeemResponse)
def redeem(req: RedeemRequest) -> RedeemResponse:
    try:
        vault, _ = chain.get_contracts()
        me = chain.server_address()
        shares = int(req.shares)

        quoted = chain.call(vault.functions.convertToAssets, shares)
        min_assets = _min_from_slippage(quoted, req.min_assets_out, req.max_slippage_bps)

        receipt = chain.transact(vault.functions.redeem, shares, me, min_assets)
        events = vault.events.Withdraw().process_receipt(receipt["receipt"], errors=DISCARD)
        assets_out = events[0]["args"]["assets"] if events else quoted
        return RedeemResponse(
            tx_hash=receipt["tx_hash"],
            block_number=receipt["block_number"],
            gas_used=receipt["gas_used"],
            shares_burned=str(shares),
            assets_out=str(assets_out),
            receiver=me,
        )
    except chain.ChainError as exc:
        raise _err(exc) from exc


@app.post("/faucet", response_model=TxResponse)
def faucet(req: FaucetRequest) -> TxResponse:
    """Mint mock tokens (test asset; anyone can mint by design)."""
    try:
        _, asset = chain.get_contracts()
        to = req.to or chain.server_address()
        receipt = chain.transact(asset.functions.mint, to, int(req.amount))
        return TxResponse(
            tx_hash=receipt["tx_hash"],
            block_number=receipt["block_number"],
            gas_used=receipt["gas_used"],
        )
    except chain.ChainError as exc:
        raise _err(exc) from exc


@app.get("/account/{address}", response_model=AccountState)
def account(address: str) -> AccountState:
    try:
        vault, asset = chain.get_contracts()
        shares = chain.call(vault.functions.balanceOf, address)
        return AccountState(
            address=address,
            asset_balance=str(chain.call(asset.functions.balanceOf, address)),
            share_balance=str(shares),
            share_value_assets=str(chain.call(vault.functions.convertToAssets, shares)),
        )
    except chain.ChainError as exc:
        raise _err(exc) from exc


# (end of routes)
