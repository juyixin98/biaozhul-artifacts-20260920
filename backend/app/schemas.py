"""Pydantic request/response schemas. All token amounts are wei integers
transported as strings to avoid JSON float precision loss."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _validate_uint(name: str):
    def _check(v: str) -> str:
        if not v.isdigit():
            raise ValueError(f"{name} must be a non-negative integer string (wei)")
        return v

    return field_validator(name, mode="before")(_check)


class DepositRequest(BaseModel):
    assets: str = Field(..., description="asset amount in wei (integer string)")
    min_shares_out: str | None = Field(
        None, description="minimum shares to mint; reverts below this (max slippage)"
    )
    max_slippage_bps: int | None = Field(
        None, ge=0, le=10_000, description="alternative: derive min from preview"
    )

    _v_assets = _validate_uint("assets")
    _v_min = _validate_uint("min_shares_out")


class RedeemRequest(BaseModel):
    shares: str = Field(..., description="share amount in wei (integer string)")
    min_assets_out: str | None = Field(
        None, description="minimum assets to receive; reverts below this (max slippage)"
    )
    max_slippage_bps: int | None = Field(None, ge=0, le=10_000)

    _v_shares = _validate_uint("shares")
    _v_min = _validate_uint("min_assets_out")


class FaucetRequest(BaseModel):
    to: str | None = Field(None, description="recipient; defaults to the server account")
    amount: str = Field("1000000000000000000000", description="mock tokens to mint (wei)")

    _v_amount = _validate_uint("amount")


class TxResponse(BaseModel):
    tx_hash: str
    block_number: int
    gas_used: int


class DepositResponse(TxResponse):
    assets_in: str
    shares_minted: str
    receiver: str


class RedeemResponse(TxResponse):
    shares_burned: str
    assets_out: str
    receiver: str


class VaultState(BaseModel):
    vault: str
    asset: str
    total_assets: str
    total_supply: str
    assets_per_share: str
    virtual_share_offset: str
    rounding: dict


class PreviewDeposit(BaseModel):
    assets: str
    shares: str


class PreviewRedeem(BaseModel):
    shares: str
    assets: str


class AccountState(BaseModel):
    address: str
    asset_balance: str
    share_balance: str
    share_value_assets: str
