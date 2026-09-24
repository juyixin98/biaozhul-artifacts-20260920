"""分配表加载与校验。"""

from __future__ import annotations

import json
from pathlib import Path

from eth_utils import is_checksum_address, to_checksum_address
from pydantic import BaseModel, field_validator


class Allocation(BaseModel):
    index: int
    account: str
    amount_wei: int

    @field_validator("index")
    @classmethod
    def _non_negative_index(cls, v: int) -> int:
        if v < 0:
            raise ValueError("index must be non-negative")
        return v

    @field_validator("account")
    @classmethod
    def _checksum(cls, v: str) -> str:
        if not is_checksum_address(v):
            # 宽松接受小写地址，统一转 EIP-55
            try:
                return to_checksum_address(v)
            except Exception as exc:  # pragma: no cover - 依赖 eth_utils 抛错
                raise ValueError(f"invalid address: {v}") from exc
        return v

    @field_validator("amount_wei")
    @classmethod
    def _positive_amount(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_wei must be > 0")
        return v


def load_allocations(path: str | Path) -> list[Allocation]:
    """读取并校验 allocations.json；要求 index 唯一、连续从 0 开始。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = [Allocation.model_validate(item) for item in raw["allocations"]]
    if not items:
        raise ValueError("allocations must not be empty")

    indexes = sorted(a.index for a in items)
    if indexes != list(range(len(items))):
        raise ValueError(
            f"indexes must be unique and contiguous 0..{len(items) - 1}, got {indexes}"
        )

    accounts = [a.account for a in items]
    if len(set(accounts)) != len(accounts):
        raise ValueError("duplicate account in allocations")

    # 按 index 顺序返回，保证叶子顺序确定
    return sorted(items, key=lambda a: a.index)
