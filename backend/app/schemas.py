"""请求/响应模型。所有金额均为资产最小单位（wei）的整数字符串，绝不使用浮点。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class AccountInfo(BaseModel):
    address: str
    token_balance: int = Field(description="模拟资产余额（最小单位，整数）")
    share_balance: int = Field(description="金库份额余额（最小单位，整数）")
    token_allowance: int = Field(description="对金库的资产授权额度")


class VaultInfo(BaseModel):
    vault_address: str
    token_address: str
    chain_id: int
    total_assets: int = Field(description="金库持有资产总量")
    total_shares: int = Field(description="份额总供给（含 1000 死份额）")
    exchange_rate_x1e18: int = Field(
        description="汇率 totalAssets/totalShares，定点 1e18，整数向下取整；空库为 0"
    )
    dead_shares: int = Field(description="永久锁死的最小流动性份额")


class QuoteResponse(BaseModel):
    kind: str = Field(description="deposit 或 redeem")
    input_amount: int
    estimated_output: int = Field(description="链上 preview 结果（整数，向下取整）")
    min_accepted_output: int = Field(description="按滑点计算的链上最小接受量")
    slippage_bps: int


class TxResponse(BaseModel):
    transaction_hash: str
    block_number: int
    gas_used: int
    shares: int | None = None
    assets: int | None = None
    from_address: str


class MintRequest(BaseModel):
    address: str = Field(description="接收测试代币的地址")
    amount: int = Field(gt=0, description="铸造数量（最小单位，整数）")
    private_key: str | None = Field(
        default=None,
        description="调用者私钥；mint 是 faucet 接口，任何地址都可作为 to，故可默认用操作者",
    )


class ApproveRequest(BaseModel):
    private_key: str
    amount: int = Field(gt=0)


class DepositRequest(BaseModel):
    private_key: str
    assets: int = Field(gt=0, description="存入资产数量（最小单位，整数）")
    receiver: str | None = Field(default=None, description="份额接收地址，默认交易发送者")
    min_shares: int | None = Field(
        default=None, description="最少接受份额；不传则按 slippage_bps 自动计算"
    )
    slippage_bps: int = Field(default=50, ge=0, lt=10_000)


class RedeemRequest(BaseModel):
    private_key: str
    shares: int = Field(gt=0, description="赎回的份额数量（最小单位，整数）")
    receiver: str | None = None
    owner: str | None = Field(default=None, description="份额持有人，默认交易发送者")
    min_assets_out: int | None = None
    slippage_bps: int = Field(default=50, ge=0, lt=10_000)
