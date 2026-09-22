"""Pydantic 请求模型与字段校验。所有字节字段以十六进制字符串收发。"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class VoteIn(BaseModel):
    chain_id: str = Field(min_length=1, max_length=128)
    validator_pubkey: str
    round: int = Field(ge=0)
    block_hash: str
    signature: str

    @field_validator("validator_pubkey")
    @classmethod
    def _pub32(cls, v: str) -> str:
        b = bytes.fromhex(v)
        if len(b) != 32:
            raise ValueError("validator_pubkey 必须是 32 字节 Ed25519 公钥")
        return v

    @field_validator("signature")
    @classmethod
    def _sig64(cls, v: str) -> str:
        b = bytes.fromhex(v)
        if len(b) != 64:
            raise ValueError("signature 必须是 64 字节 Ed25519 签名")
        return v

    @field_validator("block_hash")
    @classmethod
    def _hash_nonempty(cls, v: str) -> str:
        b = bytes.fromhex(v)
        if not b:
            raise ValueError("block_hash 不能为空")
        return v


class ChainIn(BaseModel):
    chain_id: str = Field(min_length=1, max_length=128)
    epoch_length: int = Field(gt=0)


class ValidatorIn(BaseModel):
    validator_pubkey: str
    moniker: str = ""

    @field_validator("validator_pubkey")
    @classmethod
    def _pub32(cls, v: str) -> str:
        b = bytes.fromhex(v)
        if len(b) != 32:
            raise ValueError("validator_pubkey 必须是 32 字节 Ed25519 公钥")
        return v


class PowerIn(BaseModel):
    power: int = Field(ge=0)
