"""容器往返、分块边界、截断与各类篡改攻击测试。"""

from __future__ import annotations

import base64
import json
import os

import pytest

from envelope_enc.container import (
    DEFAULT_CHUNK_SIZE,
    IntegrityError,
    KeyNotFoundError,
    TruncatedContainer,
    decrypt_bytes,
    decrypt_file,
    encrypt_bytes,
    encrypt_file,
    parse_header,
)
from envelope_enc.keyring import Keyring


# --------------------------------------------------------------------- 往返
def test_roundtrip_empty_file(keyring):
    blob = encrypt_bytes(b"", keyring)
    assert decrypt_bytes(blob, keyring) == b""
    header = parse_header(blob)
    assert header["chunk_count"] == 0
    assert header["plaintext_size"] == 0


@pytest.mark.parametrize("size", [1, 16, 100, 4095, 4096, 4097, 65535, 65536, 65537, 200000])
def test_roundtrip_various_sizes(keyring, size):
    data = bytes((i * 7 + 3) & 0xFF for i in range(size))
    blob = encrypt_bytes(data, keyring, chunk_size=4096)
    assert decrypt_bytes(blob, keyring) == data


def test_chunk_count_and_final_chunk_length(keyring):
    # 65537 字节 / 64KiB -> 2 块（最后一块 1 字节）
    blob = encrypt_bytes(b"a" * 65537, keyring)
    header = parse_header(blob)
    assert header["chunk_count"] == 2
    assert header["chunk_size"] == DEFAULT_CHUNK_SIZE
    assert header["plaintext_size"] == 65537


def test_file_roundtrip_and_no_plaintext_temp_left(keyring, make_path):
    src, enc, dec = make_path("plain.bin"), make_path("plain.bin.enc"), make_path("out.bin")
    payload = os.urandom(50_000)
    with open(src, "wb") as f:
        f.write(payload)

    encrypt_file(src, enc, keyring)
    # 加密后工作目录里不应出现任何加密临时文件
    assert not os.path.exists(enc + ".enc.tmp")
    decrypt_file(enc, dec, keyring)
    with open(dec, "rb") as f:
        assert f.read() == payload
    # 解密临时文件应已改名消失
    assert not os.path.exists(dec + ".part")


def test_encrypted_file_has_no_plaintext_inside(keyring, make_path):
    """密文容器任何位置都不应包含完整明文（防止意外落明文）。"""
    src, enc = make_path("plain.bin"), make_path("plain.bin.enc")
    payload = b"ENVELOPE-SECRET-MARKER" * 100
    with open(src, "wb") as f:
        f.write(payload)
    encrypt_file(src, enc, keyring)
    with open(enc, "rb") as f:
        blob = f.read()
    assert payload not in blob


# ----------------------------------------------------------- nonce 唯一性
def test_nonces_unique_within_file(keyring):
    chunk_size = 4096
    blob = encrypt_bytes(b"x" * (chunk_size * 3 + 1), keyring, chunk_size=chunk_size)
    header = parse_header(blob)
    body = blob[11 + _header_len(blob):]
    prefix = base64.b64decode(header["nonce_prefix_b64"])

    nonces = []
    pos = 0
    for i in range(4):
        n = body[pos:pos + 12]
        nonces.append(n)
        pt_len = min(chunk_size, header["plaintext_size"] - i * chunk_size)
        pos += 12 + pt_len + 16  # nonce + 密文（含 16 字节标签）

    assert all(n[:4] == prefix for n in nonces)
    assert [int.from_bytes(n[4:], "big") for n in nonces] == [0, 1, 2, 3]
    assert len(set(nonces)) == 4
    assert pos == len(body)  # 恰好读完整个块区


def test_nonces_unique_across_files(keyring):
    # 不同文件即使内容相同，随机前缀（以压倒性概率）也不同
    b1 = encrypt_bytes(b"same" * 100, keyring, chunk_size=64)
    b2 = encrypt_bytes(b"same" * 100, keyring, chunk_size=64)
    p1 = base64.b64decode(parse_header(b1)["nonce_prefix_b64"])
    p2 = base64.b64decode(parse_header(b2)["nonce_prefix_b64"])
    assert p1 != p2
    # file_id 也应不同
    assert parse_header(b1)["file_id"] != parse_header(b2)["file_id"]


# ----------------------------------------------------------- 篡改：密文块
def test_flip_ciphertext_byte_fails(keyring):
    blob = bytearray(encrypt_bytes(b"hello world" * 100, keyring, chunk_size=16))
    # 翻转块区域中的一个字节（跳过头部，定位到第一个密文块中间）
    offset = 11 + _header_len(bytes(blob)) + 12 + 5
    blob[offset] ^= 0xFF
    with pytest.raises(IntegrityError, match="认证失败"):
        decrypt_bytes(bytes(blob), keyring)


def test_swap_chunks_between_files_fails(keyring):
    """把 A 文件的整块（nonce+密文）换到 B 文件：file_id AAD 不匹配。"""
    data_a = b"AAAA" * 50
    data_b = b"BBBB" * 50
    a = encrypt_bytes(data_a, keyring, chunk_size=16)
    b = encrypt_bytes(data_b, keyring, chunk_size=16)
    ha, hb = _body_offsets(a), _body_offsets(b)
    chunk = a[ha:ha + 12 + 16 + 16]  # 16 字节明文 -> 32 字节密文
    tampered = bytearray(b)
    tampered[hb:hb + len(chunk)] = chunk
    with pytest.raises(IntegrityError, match="认证失败"):
        decrypt_bytes(bytes(tampered), keyring)


def test_swap_chunks_within_same_file_fails(keyring):
    """同一文件内交换第 0、1 块：块序号 AAD 不匹配。"""
    blob = encrypt_bytes(b"0123456789abcdef" * 4, keyring, chunk_size=16)
    off = _body_offsets(blob)
    block_len = 12 + 16 + 16
    blocks = [blob[off + i * block_len:off + (i + 1) * block_len] for i in range(4)]
    blocks[0], blocks[1] = blocks[1], blocks[0]
    tampered = blob[:off] + b"".join(blocks)
    with pytest.raises(IntegrityError, match="认证失败"):
        decrypt_bytes(tampered, keyring)


def test_reorder_nonce_only_fails(keyring):
    """只交换两个块的 nonce（密文不动）也必须失败。"""
    blob = encrypt_bytes(b"0123456789abcdef" * 4, keyring, chunk_size=16)
    off = _body_offsets(blob)
    n0, n1 = blob[off:off + 12], blob[off + 44:off + 56]
    tampered = bytearray(blob)
    tampered[off:off + 12] = n1
    tampered[off + 44:off + 56] = n0
    with pytest.raises(IntegrityError):
        decrypt_bytes(bytes(tampered), keyring)


# ----------------------------------------------------------- 篡改：头部
def test_tamper_header_plaintext_size_fails(keyring):
    blob = encrypt_bytes(b"x" * 100, keyring, chunk_size=16)
    tampered = _patch_header(blob, {"plaintext_size": 99})
    # 99 与 chunk_count=7(按16) 不自洽 -> 结构错误；若自洽则头部标签失败
    with pytest.raises(IntegrityError):
        decrypt_bytes(tampered, keyring)


def test_tamper_header_chunk_size_fails(keyring):
    blob = encrypt_bytes(b"x" * 100, keyring, chunk_size=16)
    tampered = _patch_header(blob, {"chunk_size": 32})
    with pytest.raises(IntegrityError):
        decrypt_bytes(tampered, keyring)


def test_tamper_file_id_in_header_fails(keyring):
    blob = encrypt_bytes(b"x" * 100, keyring, chunk_size=16)
    tampered = _patch_header(blob, {"file_id": "f-deadbeef"})
    with pytest.raises(IntegrityError, match="文件头认证失败"):
        decrypt_bytes(tampered, keyring)


def test_swap_wrapped_dek_between_files_fails(keyring):
    """互换两个文件的 wrapped_dek：包裹 AAD/头 AAD 都绑定 kid 与字段，必失败。"""
    a = encrypt_bytes(b"x" * 50, keyring)
    b = encrypt_bytes(b"y" * 50, keyring)
    ha, hb = parse_header(a), parse_header(b)
    tampered = _patch_header(a, {"wrapped_dek_b64": hb["wrapped_dek_b64"]})
    with pytest.raises(IntegrityError):
        decrypt_bytes(tampered, keyring)


def test_magic_and_version_checks(keyring):
    blob = bytearray(encrypt_bytes(b"x", keyring))
    blob[0] ^= 0x01
    with pytest.raises(IntegrityError, match="magic"):
        decrypt_bytes(bytes(blob), keyring)
    blob = bytearray(encrypt_bytes(b"x", keyring))
    blob[6] = 0x09
    with pytest.raises(IntegrityError, match="版本"):
        decrypt_bytes(bytes(blob), keyring)


def test_trailing_bytes_rejected(keyring):
    blob = encrypt_bytes(b"x" * 32, keyring, chunk_size=16)
    with pytest.raises(IntegrityError, match="多余字节"):
        decrypt_bytes(blob + b"\x00", keyring)


# ------------------------------------------------------------------ 截断
def test_truncate_middle_of_block(keyring):
    blob = encrypt_bytes(b"x" * 100, keyring, chunk_size=16)
    with pytest.raises(TruncatedContainer, match="截断"):
        decrypt_bytes(blob[:-1], keyring)


def test_truncate_whole_final_block(keyring):
    blob = encrypt_bytes(b"x" * 100, keyring, chunk_size=16)
    # 砍掉最后一块的全部字节（12 nonce + 16+4 密文标签）
    with pytest.raises(TruncatedContainer, match="截断"):
        decrypt_bytes(blob[: -(12 + 20)], keyring)


def test_truncate_header(keyring):
    blob = encrypt_bytes(b"x" * 10, keyring)
    with pytest.raises(TruncatedContainer):
        decrypt_bytes(blob[:14], keyring)  # 头只读到 3 字节


def test_truncate_to_empty(keyring):
    blob = encrypt_bytes(b"x" * 10, keyring)
    with pytest.raises(TruncatedContainer):
        decrypt_bytes(b"", keyring)


def test_file_truncated_on_disk_fails_and_no_plaintext_output(keyring, make_path):
    enc, dec = make_path("x.enc"), make_path("x.bin")
    blob = encrypt_bytes(b"x" * 3000, keyring, chunk_size=64)
    with open(enc, "wb") as f:
        f.write(blob[: len(blob) // 2])  # 写一半
    with pytest.raises(TruncatedContainer):
        decrypt_file(enc, dec, keyring)
    # 失败后既不应留下 .part，也不应留下目标明文文件
    assert not os.path.exists(dec + ".part")
    assert not os.path.exists(dec)


# ------------------------------------------------------------------ 密钥
def test_decrypt_after_master_deleted_fails(tmp_path, keyring):
    blob = encrypt_bytes(b"secret", keyring)
    old_kid = parse_header(blob)["kid"]
    keyring.rotate_master()
    keyring.delete_key(old_kid)
    with pytest.raises(KeyNotFoundError):
        decrypt_bytes(blob, keyring)


def test_wrong_keyring_cannot_decrypt(tmp_path, keyring):
    blob = encrypt_bytes(b"secret", keyring)
    other = Keyring(str(tmp_path / "other.json"))
    other.initialize()
    # 不同密钥环连 kid 都对不上 -> 找不到主密钥
    with pytest.raises(KeyNotFoundError, match="找不到主密钥"):
        decrypt_bytes(blob, other)

    # 构造"kid 相同、密钥材料不同"的恶意/异环场景 -> 包裹层 AEAD 失败
    import base64
    import json

    evil_path = tmp_path / "evil.json"
    header = parse_header(blob)
    evil = {
        "keys": {
            header["kid"]: {
                "key_b64": base64.b64encode(b"\x01" * 32).decode("ascii"),
                "created_at": "",
                "status": "active",
            }
        },
        "active_kid": header["kid"],
    }
    evil_path.write_text(json.dumps(evil), encoding="utf-8")
    with pytest.raises(IntegrityError, match="包裹层认证失败"):
        decrypt_bytes(blob, Keyring(str(evil_path)))


# ------------------------------------------------------------------ 辅助
def _header_len(blob: bytes) -> int:
    return int.from_bytes(blob[7:11], "big")


def _body_offsets(blob: bytes) -> int:
    return 11 + _header_len(blob)


def _patch_header(blob: bytes, changes: dict) -> bytes:
    """重写头部 JSON（长度字段同步更新），块区字节原样保留。"""
    hl = _header_len(blob)
    header = json.loads(blob[11:11 + hl].decode("utf-8"))
    header.update(changes)
    new_raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return blob[:7] + len(new_raw).to_bytes(4, "big") + new_raw + blob[11 + hl:]
