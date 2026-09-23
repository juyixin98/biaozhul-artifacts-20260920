"""Pydantic v2 数据模型（API 边界校验）。"""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX_SIG = re.compile(r"^[0-9a-f]{128,144}$")  # Ed25519 签名 64B；宽松上限防垃圾


class ValidatorSpec(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    weight: int = Field(ge=0)
    public_key: str = Field(min_length=64, max_length=128)


class CreateEpochRequest(BaseModel):
    epoch: int = Field(ge=0)
    validators: list[ValidatorSpec] = Field(min_length=1)

    @field_validator("validators")
    @classmethod
    def _unique_ids(cls, v: list[ValidatorSpec]) -> list[ValidatorSpec]:
        ids = [x.id for x in v]
        if len(set(ids)) != len(ids):
            raise ValueError("同一 epoch 内验证者 id 不得重复")
        keys = [x.public_key for x in v]
        if len(set(keys)) != len(keys):
            raise ValueError("同一 epoch 内验证者公钥不得重复")
        return v


class VoteIn(BaseModel):
    epoch: int = Field(ge=0)
    validator_id: str = Field(min_length=1, max_length=128)
    block_hash: str
    signature: str

    @field_validator("block_hash")
    @classmethod
    def _block_hash_is_hex64(cls, v: str) -> str:
        if not _HEX64.match(v):
            raise ValueError("block_hash 必须是 64 位小写十六进制（32 字节哈希）")
        return v

    @field_validator("signature")
    @classmethod
    def _sig_is_hex(cls, v: str) -> str:
        if not _HEX_SIG.match(v):
            raise ValueError("signature 必须是十六进制编码的 Ed25519 签名（64 字节=128 hex）")
        return v


class TallyBlock(BaseModel):
    block_hash: str
    weight: int
    validators: list[str]


class EquivocationOut(BaseModel):
    epoch: int
    validator_id: str
    weight: int
    block_hashes: list[str]
    vote_ids: list[int]


class ConflictOut(BaseModel):
    reason: str
    finalized_block: str
    conflicting_block: str
    finalized_weight: int
    conflicting_apparent_weight: int
    equivocators: list[str]
    votes_for: dict[str, list[int]]


class EpochStatus(BaseModel):
    epoch: int
    state: Literal["pending", "finalized", "frozen_conflict"]
    total_weight: int
    required_weight: int
    accepted_votes: int
    finalized_block: Optional[str]
    finalized_weight: Optional[int]
    equivocations: list[EquivocationOut]
    effective_tally: list[TallyBlock]
    apparent_tally: list[TallyBlock]
    conflict: Optional[ConflictOut]


class AcceptedOut(BaseModel):
    accepted: bool
    stored: bool
    reason: Optional[str]
    vote_id: Optional[int]
    epoch_state: str


class CheckpointOut(BaseModel):
    epoch: int
    block_hash: str
    weight: int
    required_weight: int
    total_weight: int


class EpochSummary(BaseModel):
    epoch: int
    state: str
    total_weight: int
    finalized_block: Optional[str]
