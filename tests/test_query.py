"""查询验证测试：游标、范围反转、失败类别。"""
from __future__ import annotations

import pytest

from app.errors import ErrorCategory, ServiceError
from app.query import Cursor, QueryService, decode_cursor, encode_cursor


def test_cursor_roundtrip(kernel):
    cursor = Cursor(kernel.index_version, b"\x01\x02\xff", 7)
    token = encode_cursor(cursor)
    decoded = decode_cursor(token, kernel.index_version)
    assert decoded == cursor


def test_malformed_cursor_rejected(kernel):
    with pytest.raises(ServiceError) as excinfo:
        decode_cursor("not-a-cursor", kernel.index_version)
    assert excinfo.value.category == ErrorCategory.VALIDATION_ERROR


def test_stale_cursor_rejected(kernel):
    token = encode_cursor(Cursor("icu74.2-oldversion", b"\x01", 0))
    with pytest.raises(ServiceError) as excinfo:
        decode_cursor(token, kernel.index_version)
    assert excinfo.value.category == ErrorCategory.STALE_CURSOR
    assert excinfo.value.detail["cursor_version"] == "icu74.2-oldversion"


def test_range_inversion_detected(index, kernel, sample_entries):
    """lower='z' / upper='é'：UTF-8 下看似合法，排序键意义下反转。"""
    index.rebuild(kernel, sample_entries)
    service = QueryService(kernel, index)
    with pytest.raises(ServiceError) as excinfo:
        service.range_query(
            "z", "é",
            lower_inclusive=True, upper_inclusive=True, limit=10,
            request_id="test-req",
        )
    assert excinfo.value.category == ErrorCategory.RANGE_INVERSION
    assert excinfo.value.http_status == 422


def test_limit_validation(index, kernel, sample_entries):
    index.rebuild(kernel, sample_entries)
    service = QueryService(kernel, index)
    with pytest.raises(ServiceError) as excinfo:
        service.sorted_page(0, None, "test-req")
    assert excinfo.value.category == ErrorCategory.VALIDATION_ERROR


def test_pagination_covers_all_entries_exactly_once(index, kernel, sample_entries):
    index.rebuild(kernel, sample_entries)
    service = QueryService(kernel, index)
    seen: list[str] = []
    cursor = None
    while True:
        page = service.sorted_page(5, cursor, "test-req")
        seen.extend(e.id for e in page.entries)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    assert len(seen) == len(sample_entries)
    assert len(set(seen)) == len(seen)  # 无重复、无遗漏
