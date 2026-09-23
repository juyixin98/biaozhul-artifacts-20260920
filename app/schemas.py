"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, StrictInt, StrictStr


class ChainIn(BaseModel):
    chain_id: StrictStr = Field(min_length=1, max_length=128)


class ValidatorIn(BaseModel):
    chain_id: StrictStr = Field(min_length=1, max_length=128)
    public_key: StrictStr = Field(min_length=64, max_length=64)
    moniker: StrictStr = ""


class SnapshotIn(BaseModel):
    chain_id: StrictStr = Field(min_length=1, max_length=128)
    validator_address: StrictStr = Field(min_length=40, max_length=40)
    epoch: StrictInt = Field(ge=0)
    voting_power: StrictInt = Field(ge=0)


class VoteIn(BaseModel):
    chain_id: StrictStr = Field(min_length=1, max_length=128)
    validator_address: StrictStr = Field(min_length=40, max_length=40)
    height: StrictInt = Field(ge=0)
    round: StrictInt = Field(ge=0)
    vote_type: Literal["prevote", "precommit"]
    block_hash: StrictStr = Field(min_length=64, max_length=64)
    public_key: StrictStr = Field(min_length=64, max_length=64)
    signature: StrictStr = Field(min_length=128, max_length=128)
