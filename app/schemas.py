from __future__ import annotations

import datetime as dt
import re
from typing import Optional

from eth_utils import is_address, is_checksum_address, to_checksum_address
from pydantic import BaseModel, ConfigDict, Field, field_validator

_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_HEX_DATA_RE = re.compile(r"^0[xX]([0-9a-fA-F]{2})*$")


def checksum_address(value: str, field_name: str = "address") -> str:
    if not isinstance(value, str) or not is_address(value):
        raise ValueError(f"{field_name} must be a valid 20-byte EVM address")
    # Reject all-lower/non-checksumed mixed-case forms: EIP-55 checksum required.
    if value != value.lower() and value != value.upper() and not is_checksum_address(value):
        raise ValueError(f"{field_name} has a bad EIP-55 checksum")
    return to_checksum_address(value)


# ---------------------------------------------------------------- users ----

class UserCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: dt.datetime
    # Present EXACTLY once, on registration; never shown again.
    api_key: Optional[str] = None


# -------------------------------------------------------------- wallets ----

class WalletCreate(BaseModel):
    label: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern="^(custodial|watch_only)$")
    # Required for custodial wallets. Never echoed back by the API.
    private_key_hex: Optional[str] = Field(default=None, max_length=66)
    # Required for watch-only wallets.
    address: Optional[str] = Field(default=None, max_length=42)

    @field_validator("address")
    @classmethod
    def _check_addr(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return checksum_address(v, "address")


class WalletOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    label: str
    kind: str
    address: str
    created_at: dt.datetime
    has_private_key: bool


# ---------------------------------------------------------------- drafts ----

class DraftContent(BaseModel):
    chain_id: int = Field(gt=0, le=2**63 - 1)
    to_address: str = Field(max_length=42)
    value_wei: int = Field(ge=0, le=2**256 - 1)
    gas: int = Field(gt=0, le=2**63 - 1)
    gas_price_wei: int = Field(ge=0, le=2**256 - 1)
    nonce: int = Field(ge=0, le=2**63 - 1)
    data_hex: Optional[str] = Field(default="0x", max_length=200_000)

    @field_validator("to_address")
    @classmethod
    def _to(cls, v: str) -> str:
        return checksum_address(v, "to_address")

    @field_validator("data_hex")
    @classmethod
    def _data(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return "0x"
        if not _HEX_DATA_RE.match(v):
            raise ValueError("data_hex must be 0x-prefixed even-length hex")
        return v.lower()


class DraftCreate(DraftContent):
    wallet_id: str


class DraftPatch(BaseModel):
    chain_id: Optional[int] = Field(default=None, gt=0, le=2**63 - 1)
    to_address: Optional[str] = Field(default=None, max_length=42)
    value_wei: Optional[int] = Field(default=None, ge=0, le=2**256 - 1)
    gas: Optional[int] = Field(default=None, gt=0, le=2**63 - 1)
    gas_price_wei: Optional[int] = Field(default=None, ge=0, le=2**256 - 1)
    nonce: Optional[int] = Field(default=None, ge=0, le=2**63 - 1)
    data_hex: Optional[str] = Field(default=None, max_length=200_000)

    @field_validator("to_address")
    @classmethod
    def _to(cls, v: Optional[str]) -> Optional[str]:
        return checksum_address(v, "to_address") if v is not None else None

    @field_validator("data_hex")
    @classmethod
    def _data(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _HEX_DATA_RE.match(v):
            raise ValueError("data_hex must be 0x-prefixed even-length hex")
        return v.lower() if v is not None else None


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    wallet_id: str
    status: str
    chain_id: int
    to_address: str
    value_wei: int
    gas: int
    gas_price_wei: int
    nonce: int
    data_hex: Optional[str]
    frozen_at: Optional[dt.datetime]
    created_at: dt.datetime
    updated_at: dt.datetime


# ---------------------------------------------------------- sign requests ----

class SignSubmit(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def _idem(cls, v: str) -> str:
        if not _IDEMPOTENCY_RE.match(v):
            raise ValueError(
                "idempotency_key must be 8-128 chars of [A-Za-z0-9._:-]"
            )
        return v


class SignRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    draft_id: str
    wallet_id: str
    idempotency_key: str
    status: str
    chain_id: int
    to_address: str
    value_wei: int
    gas: int
    gas_price_wei: int
    nonce: int
    data_hex: Optional[str]
    raw_transaction_hex: Optional[str] = None
    tx_hash: Optional[str] = None
    error_reason: Optional[str] = None
    replayed: bool = False
    created_at: dt.datetime
    signed_at: Optional[dt.datetime] = None
    finished_at: Optional[dt.datetime] = None


# ----------------------------------------------------------------- audit ----

class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    action: str
    result: str
    wallet_id: Optional[str]
    sign_request_id: Optional[str]
    summary: Optional[str]
    detail: Optional[str]
    created_at: dt.datetime
