"""行序列化：BYTEA -> hex，时间 -> ISO 字符串。"""
from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any


def serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, (bytes, bytearray, memoryview)):
            out[k] = bytes(v).hex()
        elif isinstance(v, (datetime.datetime, datetime.date)):
            out[k] = v.isoformat()
        elif isinstance(v, Decimal):
            out[k] = int(v)
        else:
            out[k] = v
    return out
