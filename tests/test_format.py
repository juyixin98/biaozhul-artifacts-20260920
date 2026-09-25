"""格式层测试：固定块 AEAD、跨块范围、空对象、最后短块、密文交换、篡改。"""

from __future__ import annotations

import os

import pytest

from encrypted_range_store.format import (
    MAGIC,
    NONCE_LEN,
    TAG_LEN,
    generate_master_key,
    open_object,
    write_object,
)
from encrypted_range_store.errors import IntegrityError


def flip_byte(path: str, offset: int) -> None:
    with open(path, "r+b") as f:
        f.seek(offset)
        b = f.read(1)
        f.seek(offset)
        f.write(bytes([b[0] ^ 0xFF]))


def truncate_to(path: str, length: int) -> None:
    with open(path, "r+b") as f:
        f.truncate(length)


def append_bytes(path: str, payload: bytes) -> None:
    with open(path, "ab") as f:
        f.write(payload)


def test_roundtrip_small(tmp_path):
    key = generate_master_key()
    path = str(tmp_path / "o")
    data = b"hello encrypted range"
    write_object(path, "o", data, key)
    with open_object(path, "o", key) as r:
        assert r.plaintext_len == len(data)
        assert r.read_range(0, len(data)) == data
        assert r.read_range(6, 15) == b"encrypted"


def test_multi_block_range_and_full(tmp_path):
    bs = 100
    key = generate_master_key()
    path = str(tmp_path / "big")
    data = bytes(range(256)) * 4  # 1024 字节 = 10 个满块 + 24 字节短块
    write_object(path, "big", data, key, block_size=bs)
    with open_object(path, "big", key) as r:
        assert r.plaintext_len == 1024
        # 首范围（含开头）
        assert r.read_range(0, 10) == data[:10]
        # 尾范围：落在最后的短块
        assert r.read_range(1000, 1024) == data[1000:1024]
        assert r.read_range(1023, 1024) == data[1023:1024]
        # 跨多个块的范围
        assert r.read_range(90, 310) == data[90:310]
        # 空区间
        assert r.read_range(50, 50) == b""
        # 整读
        assert r.read_range(0, 1024) == data
        # 逐块与最后短块长度
        assert len(r.read_block(9)) == 100
        assert len(r.read_block(10)) == 24


def test_empty_object(tmp_path):
    key = generate_master_key()
    path = str(tmp_path / "empty")
    write_object(path, "empty", b"", key)
    assert os.path.getsize(path) > 0  # 头 + 加密清单仍然存在
    with open_object(path, "empty", key) as r:
        assert r.plaintext_len == 0
        assert r.read_range(0, 0) == b""
        with pytest.raises(IntegrityError):
            r.read_block(0)


def test_each_byte_position_roundtrips(tmp_path):
    """逐字节起点 / 终点的小块穷举，确保边界切片正确。"""
    bs = 7
    key = generate_master_key()
    path = str(tmp_path / "x")
    data = b"abcdefghijklmnopqrstu"  # 21 字节
    write_object(path, "x", data, key, block_size=bs)
    with open_object(path, "x", key) as r:
        for i in range(len(data) + 1):
            for j in range(i, len(data) + 1):
                assert r.read_range(i, j) == data[i:j]


def test_tamper_middle_block_returns_nothing(tmp_path):
    """跨块范围中第二块被篡改：整体失败，拿不到第一块已解密的字节。"""
    bs = 16
    key = generate_master_key()
    path = str(tmp_path / "t")
    data = bytes(range(64))
    write_object(path, "t", data, key, block_size=bs)

    with open_object(path, "t", key) as r:
        second_block_offset = r._blocks[1].offset

    # 篡改第二块的密文区（跳过 nonce 与长度字段）
    flip_byte(path, second_block_offset + NONCE_LEN + 4 + 3)

    with open_object(path, "t", key) as r:
        with pytest.raises(IntegrityError):
            r.read_range(0, 64)
        with pytest.raises(IntegrityError):
            r.read_range(16, 32)
        # 未受影响的块仍可正常读取（认证通过才返回）
        assert r.read_block(0) == data[:16]


def test_tamper_nonce(tmp_path):
    bs = 16
    key = generate_master_key()
    path = str(tmp_path / "t")
    write_object(path, "t", bytes(range(32)), key, block_size=bs)
    with open_object(path, "t", key) as r:
        off = r._blocks[0].offset
    flip_byte(path, off + 5)  # nonce 内
    with open_object(path, "t", key) as r:
        with pytest.raises(IntegrityError):
            r.read_block(0)


def test_tamper_manifest(tmp_path):
    key = generate_master_key()
    path = str(tmp_path / "m")
    write_object(path, "m", b"abc", key)
    # 清单密文在头部之后：魔数8 + 版本1 + 盐长1 + 盐16 + 长度8 + nonce12
    flip_byte(path, 8 + 1 + 1 + 16 + 8 + 12 + 2)
    with pytest.raises(IntegrityError):
        open_object(path, "m", key)


def test_truncation_detected(tmp_path):
    bs = 16
    key = generate_master_key()
    path = str(tmp_path / "tr")
    write_object(path, "tr", bytes(range(64)), key, block_size=bs)
    size = os.path.getsize(path)
    truncate_to(path, size - 5)
    with open_object(path, "tr", key) as r:
        # 打开时遍历块头即可发现最后一块被截断
        with pytest.raises(IntegrityError):
            r.read_block(3)


def test_trailing_bytes_detected(tmp_path):
    key = generate_master_key()
    path = str(tmp_path / "ap")
    write_object(path, "ap", b"abc", key)
    append_bytes(path, b"\x00\x00")
    with pytest.raises(IntegrityError):
        open_object(path, "ap", key)


def test_bad_magic_and_version(tmp_path):
    key = generate_master_key()
    path = str(tmp_path / "b")
    write_object(path, "b", b"abc", key)
    flip_byte(path, 0)
    with pytest.raises(IntegrityError):
        open_object(path, "b", key)


def test_wrong_master_key(tmp_path):
    path = str(tmp_path / "k")
    write_object(path, "k", b"abc", generate_master_key())
    with pytest.raises(IntegrityError):
        open_object(path, "k", generate_master_key())


def test_swap_whole_files_between_object_ids(tmp_path):
    """把对象 B 的整份密文以对象 A 的名字打开：清单 id 不符必须拒绝。"""
    key = generate_master_key()
    pa = str(tmp_path / "A")
    pb = str(tmp_path / "B")
    write_object(pa, "A", b"payload-A" * 20, key, block_size=16)
    write_object(pb, "B", b"payload-B-different" * 10, key, block_size=16)

    # 攻击者把 B 的文件复制到 A 的路径（同主密钥、不同盐）。
    with open(pb, "rb") as f:
        blob_b = f.read()
    with open(pa, "wb") as f:
        f.write(blob_b)

    with pytest.raises(IntegrityError):
        open_object(pa, "A", key)
    # B 本身仍然完好
    with open_object(pb, "B", key) as r:
        assert r.read_range(0, r.plaintext_len).startswith(b"payload-B")


def test_swap_blocks_within_file(tmp_path):
    """同一文件内交换两个块：AAD 绑定块索引与文件偏移，认证必须失败。"""
    bs = 16
    key = generate_master_key()
    path = str(tmp_path / "swap")
    data = bytes(range(64))
    write_object(path, "swap", data, key, block_size=bs)

    with open_object(path, "swap", key) as r:
        off0, off2 = r._blocks[0].offset, r._blocks[2].offset
        record_len = NONCE_LEN + 4 + bs + TAG_LEN

    with open(path, "rb") as f:
        blob = bytearray(f.read())
    rec0 = bytes(blob[off0:off0 + record_len])
    rec2 = bytes(blob[off2:off2 + record_len])
    blob[off0:off0 + record_len] = rec2
    blob[off2:off2 + record_len] = rec0
    with open(path, "wb") as f:
        f.write(blob)

    with open_object(path, "swap", key) as r:
        with pytest.raises(IntegrityError):
            r.read_block(0)
        with pytest.raises(IntegrityError):
            r.read_block(2)


def test_swap_block_across_files_same_position(tmp_path):
    """不同对象、相同块索引 / 相同长度的块互换：
    对象标识（以及不同盐派生的块密钥）使认证失败。"""
    bs = 16
    key = generate_master_key()
    pa = str(tmp_path / "aa")
    pb = str(tmp_path / "bb")
    write_object(pa, "aa", b"A" * 48, key, block_size=bs)
    write_object(pb, "bb", b"B" * 48, key, block_size=bs)

    with open_object(pa, "aa", key) as ra, open_object(pb, "bb", key) as rb:
        off_a0 = ra._blocks[0].offset
        off_b0 = rb._blocks[0].offset
    record_len = NONCE_LEN + 4 + bs + TAG_LEN

    with open(pa, "rb") as f:
        a = bytearray(f.read())
    with open(pb, "rb") as f:
        b = bytearray(f.read())
    a[off_a0:off_a0 + record_len] = b[off_b0:off_b0 + record_len]
    with open(pa, "wb") as f:
        f.write(a)

    with open_object(pa, "aa", key) as r:
        with pytest.raises(IntegrityError):
            r.read_block(0)


def test_move_block_to_different_offset_fails(tmp_path):
    """把同一块记录复制到另一个块位置（内容相同），偏移 AAD 不同应失败。"""
    bs = 16
    key = generate_master_key()
    path = str(tmp_path / "mov")
    write_object(path, "mov", b"Z" * 48, key, block_size=bs)
    with open_object(path, "mov", key) as r:
        off0, off1 = r._blocks[0].offset, r._blocks[1].offset
    record_len = NONCE_LEN + 4 + bs + TAG_LEN
    with open(path, "rb") as f:
        blob = bytearray(f.read())
    blob[off1:off1 + record_len] = blob[off0:off0 + record_len]
    with open(path, "wb") as f:
        f.write(blob)
    with open_object(path, "mov", key) as r:
        with pytest.raises(IntegrityError):
            r.read_block(1)


def test_magic_constant():
    assert MAGIC == b"ENCRRNG1"
