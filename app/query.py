"""查询验证：游标编解码、范围边界校验、分页与范围查询编排。

游标是不透明令牌，内嵌索引版本与位置 (sort_key, seq)。
版本不匹配的游标一律拒绝（STALE_CURSOR），绝不跨版本混用。
范围边界先经内核转为排序键再比较 —— UTF-8 序与排序序不同
（例如 'é' 在 UTF-8 下大于 'z'，在 en_US 排序下小于 'z'）。
"""
from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from dataclasses import dataclass

from .errors import ServiceError, range_inversion, stale_cursor, validation_error
from .index import SQLiteIndex
from .kernel import CollationKernel
from .logging_setup import log_step
from .models import EntryModel, RangeResult, SortedPage

MAX_LIMIT = 500
_CURSOR_KIND = "collationsvc-cursor"


@dataclass(frozen=True)
class Cursor:
    index_version: str
    sort_key: bytes
    seq: int


def encode_cursor(cursor: Cursor) -> str:
    payload = {
        "kind": _CURSOR_KIND,
        "v": cursor.index_version,
        "k": cursor.sort_key.hex(),
        "s": cursor.seq,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(token: str, expected_version: str) -> Cursor:
    """解码并校验游标。失败类别：VALIDATION_ERROR / STALE_CURSOR。"""
    try:
        payload = json.loads(base64.urlsafe_b64decode(token.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise validation_error("游标不是合法的 base64 JSON", cursor=token[:32])
    if not isinstance(payload, dict) or payload.get("kind") != _CURSOR_KIND:
        raise validation_error("游标结构不合法", cursor=token[:32])
    version = payload.get("v")
    if version != expected_version:
        raise stale_cursor(
            "游标来自旧索引版本，请放弃该游标并重新翻页",
            cursor_version=version,
            index_version=expected_version,
        )
    try:
        return Cursor(
            index_version=version,
            sort_key=bytes.fromhex(payload["k"]),
            seq=int(payload["s"]),
        )
    except (KeyError, ValueError, TypeError):
        raise validation_error("游标字段不合法", cursor=token[:32])


def _row_to_model(row: sqlite3.Row) -> EntryModel:
    return EntryModel(
        id=row["id"],
        seq=row["seq"],
        original=row["original"],
        nfc=row["nfc"],
        sort_key_hex=row["sort_key"].hex(),
    )


def _validate_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIMIT:
        raise validation_error(
            f"limit 必须在 1..{MAX_LIMIT} 之间", limit=limit, max_limit=MAX_LIMIT
        )
    return limit


class QueryService:
    """编排版本校验、游标处理与索引读取。"""

    def __init__(self, kernel: CollationKernel, index: SQLiteIndex) -> None:
        self.kernel = kernel
        self.index = index

    def sorted_page(
        self, limit: int, cursor_token: str | None, request_id: str
    ) -> SortedPage:
        _validate_limit(limit)
        version = self.index.require_version(self.kernel)
        after: tuple[bytes, int] | None = None
        if cursor_token is not None:
            cursor = decode_cursor(cursor_token, version)
            after = (cursor.sort_key, cursor.seq)
        # 多取一行判断是否还有下一页。
        rows = self.index.fetch_page(after, limit + 1)
        has_more = len(rows) > limit
        page = rows[:limit]
        next_cursor = None
        if has_more and page:
            last = page[-1]
            next_cursor = encode_cursor(
                Cursor(version, last["sort_key"], last["seq"])
            )
        log_step(
            request_id,
            "sorted_page",
            index_version=version,
            rows=len(page),
            has_more=has_more,
            resumed=cursor_token is not None,
        )
        return SortedPage(
            entries=[_row_to_model(r) for r in page],
            next_cursor=next_cursor,
            index_version=version,
            request_id=request_id,
        )

    def range_query(
        self,
        lower: str,
        upper: str,
        *,
        lower_inclusive: bool,
        upper_inclusive: bool,
        limit: int,
        request_id: str,
    ) -> RangeResult:
        _validate_limit(limit)
        if lower == "" or upper == "":
            raise validation_error("范围边界不能为空字符串")
        version = self.index.require_version(self.kernel)
        lower_key = self.kernel.sort_key(lower)
        upper_key = self.kernel.sort_key(upper)
        if lower_key > upper_key:
            raise range_inversion(
                "范围下界在排序键意义下大于上界",
                lower=lower,
                upper=upper,
                lower_sort_key=lower_key.hex(),
                upper_sort_key=upper_key.hex(),
            )
        rows = self.index.fetch_range(
            lower_key,
            upper_key,
            lower_inclusive=lower_inclusive,
            upper_inclusive=upper_inclusive,
            limit=limit,
        )
        log_step(
            request_id,
            "range_query",
            index_version=version,
            lower=lower,
            upper=upper,
            rows=len(rows),
        )
        return RangeResult(
            entries=[_row_to_model(r) for r in rows],
            lower_sort_key_hex=lower_key.hex(),
            upper_sort_key_hex=upper_key.hex(),
            index_version=version,
            request_id=request_id,
        )
