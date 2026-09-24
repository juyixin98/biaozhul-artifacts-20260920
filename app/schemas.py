"""Pydantic request/response models for the HTTP API."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_HEX_RE = re.compile(r"^(?:0x)?[0-9a-fA-F\s]+$")


def _parse_frame_id(value: str | int) -> int:
    if isinstance(value, int):
        return value
    text = value.strip()
    try:
        if text.lower().startswith("0x"):
            return int(text, 16)
        if text.isdigit():
            return int(text)
        return int(text.replace(" ", ""), 16)
    except ValueError as exc:
        raise ValueError(
            f"invalid frame id '{value}'; use decimal or 0x-prefixed hex"
        ) from exc


def _parse_data_hex(value: str) -> bytes:
    text = value.strip()
    if text.lower().startswith("0x"):
        text = text[2:]
    # Support both a contiguous hex string and per-byte "0xAA 0xBB" forms.
    tokens = text.replace(",", " ").split()
    cleaned = "".join(t[2:] if t.lower().startswith("0x") else t for t in tokens)
    if not cleaned:
        raise ValueError("data is empty; provide at least one byte")
    if len(cleaned) % 2 != 0:
        raise ValueError(
            f"hex data has an odd number of nibbles ({len(cleaned)}); each "
            "byte needs two hex digits"
        )
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid hex data: {exc}") from exc


class UploadDBCRequest(BaseModel):
    name: str = Field(
        ..., min_length=1, max_length=128,
        description="Unique document name, e.g. 'powertrain'.",
        examples=["powertrain"],
    )
    content: str = Field(..., min_length=1, description="Raw DBC file text.")
    replace: bool = Field(
        False, description="Replace an existing document with the same name."
    )

    @field_validator("name")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.\-]+", v):
            raise ValueError(
                "name may contain only letters, digits, '.', '_' and '-'"
            )
        return v


class DecodeRequest(BaseModel):
    frame_id: int | str = Field(
        ...,
        description="Standard 11-bit CAN frame ID, decimal or '0x' hex.",
        examples=[0x7FF],
    )
    data: str = Field(
        ...,
        description=(
            "Data bytes as hex, e.g. '01 2A FF' or '0x012aff'. Length must "
            "equal the DBC-declared DLC."
        ),
        examples=["01 64 0A D8 FF 7F 00 00"],
    )
    strict_dlc: bool = Field(
        True,
        description="Reject frames whose byte count differs from the DBC DLC.",
    )

    @field_validator("frame_id")
    @classmethod
    def _frame_id(cls, v):
        return _parse_frame_id(v)

    @field_validator("data")
    @classmethod
    def _data(cls, v):
        return _parse_data_hex(v)


class SignalOut(BaseModel):
    name: str
    raw: int = Field(..., description="Raw decoded integer (signed applied).")
    physical: float = Field(..., description="raw * factor + offset.")
    unit: str
    byte_order: Literal["intel", "motorola"]
    signed: bool
    present: bool = Field(
        ...,
        description="False for signals in multiplex branches that are not "
        "selected by the current switch value.",
    )
    mux_id: int | None
    signal_version: str = Field(..., description="sha256 of this signal def.")


class DecodeResponse(BaseModel):
    dbc_name: str
    dbc_version: str = Field(..., description="sha256 of the whole DBC def.")
    frame_id: int
    frame_id_hex: str
    message_name: str
    dlc: int
    mux_value: int | None
    signals: list[SignalOut]


class DBCSummary(BaseModel):
    name: str
    version: str
    message_count: int
    created_at: float


class DBCDetail(DBCSummary):
    content: str
    messages: list["MessageSummary"]


class MessageSummary(BaseModel):
    frame_id: int
    frame_id_hex: str
    name: str
    dlc: int
    sender: str
    signal_count: int
    mux_switch: str | None


DBCDetail.model_rebuild()


class LogEntry(BaseModel):
    dbc_name: str
    frame_id: int
    dlc: int
    data_hex: str
    message_name: str | None
    ok: bool
    error: str | None
    created_at: float
