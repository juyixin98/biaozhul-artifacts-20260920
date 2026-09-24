"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator
from web3 import Web3


class Allocation(BaseModel):
    index: int = Field(..., ge=0)
    account: str
    amount: int = Field(..., ge=0, description="wei 为单位的领取数量")

    @field_validator("account")
    @classmethod
    def _checksum(cls, v: str) -> str:
        if not Web3.is_address(v):
            raise ValueError(f"非法地址: {v}")
        return Web3.to_checksum_address(v)


class ProofItem(BaseModel):
    index: int
    account: str
    amount: int
    leaf: str
    proof: List[str]


class PrepareBatchRequest(BaseModel):
    indices: List[int] = Field(..., min_length=1)


class PreparedBatch(BaseModel):
    contract: str
    chain_id: int
    root: str
    to: str
    data: str
    value: int = 0
    gas_estimate: int
    items: List[ProofItem]
    function: str = "claimBatch"


class SubmitResponse(BaseModel):
    tx_hash: str
    block_number: int
    gas_used: int
    status: int
    items: List[ProofItem]


class StatusResponse(BaseModel):
    rpc_url: str
    chain_id: int
    contract: str
    token: str
    owner: str
    root: Optional[str]
    allocation_count: int
