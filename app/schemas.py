from typing import Literal, Optional

from eth_utils import to_checksum_address
from pydantic import BaseModel, Field, field_validator


class UserOut(BaseModel):
    user_id: str
    api_key: str


class WalletCreate(BaseModel):
    kind: Literal["custodial", "watch_only"]
    # custodial: optional import of an existing local test key; generated if omitted
    private_key: Optional[str] = None
    # watch_only: the address to observe
    address: Optional[str] = None


class WalletOut(BaseModel):
    id: str
    kind: str
    address: str
    last_signed_at: Optional[str]
    created_at: str


class DraftCreate(BaseModel):
    wallet_id: str
    chain_id: int = Field(ge=1)
    to_address: str
    value_wei: str = Field(pattern=r"^\d+$")
    gas_limit: int = Field(gt=0)
    gas_price_wei: str = Field(pattern=r"^\d+$")
    nonce: int = Field(ge=0)

    @field_validator("to_address")
    @classmethod
    def _checksum(cls, v: str) -> str:
        return to_checksum_address(v)  # raises ValueError on malformed input


class DraftOut(BaseModel):
    id: str
    wallet_id: str
    chain_id: int
    to_address: str
    value_wei: str
    gas_limit: int
    gas_price_wei: str
    nonce: int
    status: str
    content_hash: Optional[str]
    created_at: str
    submitted_at: Optional[str]


class SignRequestCreate(BaseModel):
    draft_id: str


class SignRequestOut(BaseModel):
    id: str
    draft_id: str
    wallet_id: str
    status: str
    tx_hash: Optional[str]
    signed_tx: Optional[str]
    error: Optional[str]
    quota_reserved_wei: str
    quota_released: bool
    created_at: str
    updated_at: str


class AuditOut(BaseModel):
    id: str
    wallet_id: Optional[str]
    action: str
    tx_digest: Optional[str]
    result: str
    detail: Optional[str]
    created_at: str
