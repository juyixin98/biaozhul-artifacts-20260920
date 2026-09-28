"""Cursor and index-version tests."""
from __future__ import annotations

import pytest

from collsvc.collation.versioning import IndexVersion, InvalidCursor
from collsvc.index.cursor import MODE_LIST, MODE_RANGE, Cursor


def test_cursor_roundtrip_is_opaque_string():
    cursor = Cursor(
        index_version="idx_v1_0123456789abcdef",
        mode=MODE_RANGE,
        last_sort_key=b"\x2a\x2c\x00",
        last_seq=42,
    )
    token = cursor.encode()
    assert isinstance(token, str)
    assert " " not in token
    decoded = Cursor.decode(token)
    assert decoded == cursor


def test_cursor_rejects_garbage():
    for bad in ["", "!!!", "bm90LXJlYWw=", "idx_v1_xxxxxxxxxxxxxxxx"]:
        with pytest.raises(InvalidCursor):
            Cursor.decode(bad)


def test_cursor_rejects_unknown_mode():
    with pytest.raises(InvalidCursor):
        Cursor("idx_v1_0123456789abcdef", "sideways", b"\x00", 0)


def test_index_version_token_shape_and_parse():
    version = IndexVersion.derive("a" * 64)
    assert version.version_id.startswith("idx_v1_")
    assert len(version.version_id) == len("idx_v1_") + 16
    assert IndexVersion.parse(version.as_token()).version_id == version.version_id
    with pytest.raises(InvalidCursor):
        IndexVersion.parse("idx_v9_zz")


def test_different_fingerprints_yield_different_versions():
    v1 = IndexVersion.derive("a" * 64)
    v2 = IndexVersion.derive("b" * 64)
    assert v1.version_id != v2.version_id
