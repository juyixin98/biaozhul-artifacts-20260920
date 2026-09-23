"""Pydantic 请求/响应模型与十六进制校验。"""

from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

_HEX_RE = re.compile(r"^(0x)?[0-9a-fA-F]*$")


def _hex_bytes(v: str, name: str, allow_empty: bool = True) -> str:
    if not isinstance(v, str) or not _HEX_RE.match(v):

        raise ValueError(f"{name} must be hexadecimal")
    if v.startswith("0x"):
        v = v[2:]
    if len(v) % 2:
        raise ValueError(f"{name} must have an even number of hex digits")
    if not allow_empty and v == "":
        raise ValueError(f"{name} must not be empty")
    return v.lower()


class ChannelCreateRequest(BaseModel):
    ordering: Literal["ordered", "unordered"]
    version: str = Field(min_length=1, max_length=128)
    port_a: str = "port-a"
    port_b: str = "port-b"
    channel_a: str = "channel-0"
    channel_b: str = "channel-0"


class SendPacketRequest(BaseModel):
    src_chain: str
    src_port: str
    src_channel: str
    timeout_height: int = Field(default=0, ge=0, description="目的链绝对高度；0=不限")
    timeout_time_ns: int = Field(default=0, ge=0, description="目的链绝对纳秒；0=不限")
    data_hex: str
    amount: int = Field(default=0, ge=0, description="托管代币数量（教学用整数）")

    @field_validator("data_hex")
    @classmethod
    def _data_hex(cls, v: str) -> str:
        return _hex_bytes(v, "data_hex")


class PacketMsg(BaseModel):
    src_chain: str
    src_port: str
    src_channel: str
    dst_chain: str
    dst_port: str
    dst_channel: str
    sequence: int = Field(ge=0)
    timeout_height: int = Field(ge=0)
    timeout_time_ns: int = Field(ge=0)
    data_hex: str
    amount: int = Field(default=0, ge=0)

    @field_validator("data_hex")
    @classmethod
    def _data_hex(cls, v: str) -> str:
        return _hex_bytes(v, "data_hex")


class CheckpointMsg(BaseModel):
    chain_id: str
    height: int = Field(ge=0)
    time_ns: int = Field(ge=0)
    app_hash: str
    previous_app_hash: str
    signature: str
    verify_key: str

    @field_validator("app_hash", "previous_app_hash")
    @classmethod
    def _root_hex(cls, v: str) -> str:
        return _hex_bytes(v, "app_hash", allow_empty=False)

    @field_validator("signature")
    @classmethod
    def _sig_hex(cls, v: str) -> str:
        return _hex_bytes(v, "signature", allow_empty=False)

    @field_validator("verify_key")
    @classmethod
    def _vk_hex(cls, v: str) -> str:
        return _hex_bytes(v, "verify_key", allow_empty=False)


class ProofStepMsg(BaseModel):
    level: int = Field(ge=0, le=255)
    bit: Literal[0, 1]
    sibling: str

    @field_validator("sibling")
    @classmethod
    def _sib(cls, v: str) -> str:
        return _hex_bytes(v, "sibling", allow_empty=False)


class ProofMsg(BaseModel):
    steps: List[ProofStepMsg]
    # 仅有序通道证明 nextSequenceRecv 的值时需要（中继者从查询接口获得）。
    value_hex: Optional[str] = None

    @field_validator("value_hex")
    @classmethod
    def _vh(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _hex_bytes(v, "value_hex")


class RelayRequest(BaseModel):
    packet: PacketMsg
    checkpoint: CheckpointMsg
    proof: ProofMsg


class FinalizeBlockRequest(BaseModel):
    time_ns: Optional[int] = Field(default=None, ge=0)
    height: Optional[int] = Field(default=None, ge=0)
