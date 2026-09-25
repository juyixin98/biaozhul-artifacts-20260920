"""服务层测试：Range 解析语义、对象 ID 白名单、整对象/范围读取。"""

from __future__ import annotations

import pytest

from encrypted_range_store import (
    EncryptedObjectStore,
    InvalidObjectId,
    InvalidRangeHeader,
    ObjectNotFound,
    RangeNotSatisfiable,
    generate_master_key,
    parse_range,
)


class TestParseRange:
    def test_open_ended(self):
        assert parse_range("bytes=100-", 1000) == (100, 1000)

    def test_closed(self):
        assert parse_range("bytes=0-99", 1000) == (0, 100)
        assert parse_range("bytes=10-20", 1000) == (10, 21)

    def test_end_clamped_to_length(self):
        assert parse_range("bytes=900-5000", 1000) == (900, 1000)

    def test_suffix(self):
        assert parse_range("bytes=-100", 1000) == (900, 1000)
        assert parse_range("bytes=-5000", 1000) == (0, 1000)

    def test_single_byte(self):
        assert parse_range("bytes=0-0", 1) == (0, 1)
        assert parse_range("bytes=999-999", 1000) == (999, 1000)

    def test_whitespace_tolerated(self):
        assert parse_range("bytes= 10 - 20 ", 1000) == (10, 21)

    @pytest.mark.parametrize("hdr", [
        "items=0-10",
        "bytes=0",
        "bytes=-",
        "bytes=abc-def",
        "bytes=10-5",
        "bytes=-0",
        "",
    ])
    def test_invalid_or_unsatisfiable(self, hdr):
        if hdr in ("bytes=10-5", "bytes=-0"):
            with pytest.raises(RangeNotSatisfiable):
                parse_range(hdr, 1000)
        else:
            with pytest.raises(InvalidRangeHeader):
                parse_range(hdr, 1000)

    @pytest.mark.parametrize("hdr", [
        "bytes=0-0",
        "bytes=0-",
        "bytes=-1",
    ])
    def test_empty_object_all_unsatisfiable(self, hdr):
        with pytest.raises(RangeNotSatisfiable):
            parse_range(hdr, 0)

    def test_start_at_length_unsatisfiable(self):
        with pytest.raises(RangeNotSatisfiable):
            parse_range("bytes=1000-", 1000)

    def test_multiple_ranges_rejected(self):
        with pytest.raises(InvalidRangeHeader):
            parse_range("bytes=0-10,20-30", 1000)


class TestObjectId:
    @pytest.mark.parametrize("bad", [
        "", "a/b", "..", ".", "a.b", "a b", "x" * 129,
        "../etc", "a\0b", "中文",
    ])
    def test_rejected(self, bad, tmp_path):
        store = EncryptedObjectStore(str(tmp_path), master_key=generate_master_key())
        with pytest.raises(InvalidObjectId):
            store.put(bad, b"x")
        with pytest.raises(InvalidObjectId):
            store.get(bad)

    @pytest.mark.parametrize("good", ["a", "A-Z_0", "x" * 128])
    def test_accepted(self, good, tmp_path):
        store = EncryptedObjectStore(str(tmp_path / "d"),
                                     master_key=generate_master_key())
        store.put(good, b"ok")
        assert store.get(good) == b"ok"


class TestStoreOperations:
    def test_put_get_empty(self, tmp_path):
        store = EncryptedObjectStore(str(tmp_path / "d"), block_size=8,
                                     master_key=generate_master_key())
        store.put("e", b"")
        assert store.size("e") == 0
        assert store.get("e") == b""

    def test_first_and_last_ranges(self, tmp_path):
        store = EncryptedObjectStore(str(tmp_path / "d"), block_size=16,
                                     master_key=generate_master_key())
        data = bytes(range(256))
        store.put("o", data)
        assert store.get_range("o", 0, 1) == b"\x00"
        assert store.get_range("o", 0, 16) == data[:16]
        assert store.get_range("o", 255, 256) == b"\xff"
        assert store.get_range("o", 240, 256) == data[240:]
        assert store.get_range("o", 0, 256) == data

    def test_get_missing(self, tmp_path):
        store = EncryptedObjectStore(str(tmp_path / "d"),
                                     master_key=generate_master_key())
        with pytest.raises(ObjectNotFound):
            store.get("nope")

    def test_persistence_across_instances(self, tmp_path):
        key = generate_master_key()
        d = str(tmp_path / "d")
        EncryptedObjectStore(d, block_size=16, master_key=key).put("o", b"persist")
        store2 = EncryptedObjectStore(d, block_size=16, master_key=key)
        assert store2.get("o") == b"persist"

    def test_delete(self, tmp_path):
        store = EncryptedObjectStore(str(tmp_path / "d"),
                                     master_key=generate_master_key())
        store.put("o", b"x")
        store.delete("o")
        with pytest.raises(ObjectNotFound):
            store.get("o")
