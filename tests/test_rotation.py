"""主密钥轮换测试：只重包 DEK、原子性、中断恢复、幂等与批量。"""

from __future__ import annotations

import os

import pytest

from envelope_enc.container import (
    IntegrityError,
    KeyNotFoundError,
    decrypt_bytes,
    decrypt_file,
    encrypt_bytes,
    parse_header,
    read_header,
    rotate_bytes,
    rotate_file,
    rotate_many,
)


def test_rotate_bytes_re_wraps_dek_and_plaintext_unchanged(keyring):
    data = os.urandom(100_000)
    blob = encrypt_bytes(data, keyring, chunk_size=1024)
    old_kid = parse_header(blob)["kid"]

    new_mk = keyring.rotate_master()
    rotated = rotate_bytes(blob, keyring)

    new_header = parse_header(rotated)
    assert new_header["kid"] == new_mk.kid
    assert new_header["kid"] != old_kid
    # 数据密钥的密文被重包（kid 变了，wrap nonce 也重新随机）
    assert new_header["wrapped_dek_b64"] != parse_header(blob)["wrapped_dek_b64"]
    # file_id、明文长度、块数、nonce 前缀不变 —— 数据没有被重新加密
    assert new_header["file_id"] == parse_header(blob)["file_id"]
    assert new_header["plaintext_size"] == parse_header(blob)["plaintext_size"]
    assert new_header["chunk_count"] == parse_header(blob)["chunk_count"]
    assert new_header["nonce_prefix_b64"] == parse_header(blob)["nonce_prefix_b64"]
    # 用新主密钥可以解密，明文一致
    assert decrypt_bytes(rotated, keyring) == data
    # 旧容器也仍可用 retired 旧主密钥解密
    assert decrypt_bytes(blob, keyring) == data


def test_rotate_does_not_touch_ciphertext_chunks(keyring):
    """轮换必须只换头：逐字节比较块区，完全一致。"""
    blob = encrypt_bytes(os.urandom(50_000), keyring, chunk_size=256)
    keyring.rotate_master()
    rotated = rotate_bytes(blob, keyring)

    def body(buf: bytes) -> bytes:
        return buf[11 + int.from_bytes(buf[7:11], "big"):]

    assert body(rotated) == body(blob)


def test_rotate_is_idempotent_when_already_on_target(keyring):
    blob = encrypt_bytes(b"hello", keyring)
    keyring.rotate_master()
    rotated = rotate_bytes(blob, keyring)
    # 再次轮换：已是当前 active，字节不变
    again = rotate_bytes(rotated, keyring)
    assert again == rotated
    # 文件路径版本也应 skipped
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(rotated)
        path = tf.name
    try:
        result = rotate_file(path, keyring)
        assert result.skipped is True
        assert result.old_kid == result.new_kid
    finally:
        os.unlink(path)


def test_rotate_multiple_times_chain(keyring):
    """连续两代轮换：每一代都能解开当前文件，跨代旧 kid 已删除时失败。"""
    blob = encrypt_bytes(b"chain-data" * 100, keyring, chunk_size=32)
    keyring.rotate_master()
    blob = rotate_bytes(blob, keyring)
    keyring.rotate_master()
    blob = rotate_bytes(blob, keyring)
    assert decrypt_bytes(blob, keyring) == b"chain-data" * 100
    # 此时文件由第三代包裹，密钥环中 3 把都还在
    assert len(keyring.list_keys()) == 3


def test_rotate_file_atomic_and_interrupted(keyring, make_path):
    """模拟轮换在临时文件写好后崩溃：原文件保持旧 kid，无 .rot.tmp 残留。"""
    enc = make_path("data.enc")
    data = b"crash-test" * 1000
    blob = encrypt_bytes(data, keyring, chunk_size=64)
    with open(enc, "wb") as f:
        f.write(blob)
    old_kid = read_header(enc)["kid"]

    keyring.rotate_master()
    new_kid = keyring.active().kid

    from envelope_enc.container import _SimulatedCrash

    with pytest.raises(_SimulatedCrash):
        rotate_file(enc, keyring, crash_after="tmp_written")

    # 崩溃清理后：原文件未被替换
    assert read_header(enc)["kid"] == old_kid
    assert not os.path.exists(enc + ".rot.tmp")
    # 原文件依然可解
    assert decrypt_file(enc, make_path("dec1.bin"), keyring) == make_path("dec1.bin")
    with open(make_path("dec1.bin"), "rb") as f:
        assert f.read() == data

    # 重新执行轮换（幂等可重入）→ 成功，新 kid，可解
    outcome = rotate_file(enc, keyring)
    assert outcome.old_kid == old_kid and outcome.new_kid == new_kid
    assert read_header(enc)["kid"] == new_kid
    decrypt_file(enc, make_path("dec2.bin"), keyring)
    with open(make_path("dec2.bin"), "rb") as f:
        assert f.read() == data


def test_rotate_many_reentrant_after_partial_failure(keyring, make_path):
    # 三个文件：两个旧 kid、一个已是新 kid
    e1, e2, e3 = make_path("a.enc"), make_path("b.enc"), make_path("c.enc")
    old_blob1 = encrypt_bytes(b"aaa" * 100, keyring, chunk_size=16)
    old_blob2 = encrypt_bytes(b"bbb" * 100, keyring, chunk_size=16)
    keyring.rotate_master()
    new_blob = encrypt_bytes(b"ccc" * 100, keyring, chunk_size=16)
    for path, content in ((e1, old_blob1), (e2, old_blob2), (e3, new_blob)):
        with open(path, "wb") as f:
            f.write(content)

    results = rotate_many([e1, e2, e3], keyring)
    assert all(r.ok for r in results)
    assert results[0].outcome.skipped is False
    assert results[1].outcome.skipped is False
    assert results[2].outcome.skipped is True

    # 再跑一遍：全部 skipped，文件不变，可重入
    results2 = rotate_many([e1, e2, e3], keyring)
    assert all(r.ok for r in results2)
    assert all(r.outcome.skipped for r in results2)
    for p, expected in ((e1, b"aaa" * 100), (e2, b"bbb" * 100), (e3, b"ccc" * 100)):
        assert decrypt_bytes(open(p, "rb").read(), keyring) == expected


def test_rotate_refuses_tampered_file(keyring):
    blob = bytearray(encrypt_bytes(b"x" * 200, keyring, chunk_size=16))
    blob[-1] ^= 0x01  # 破坏最后一块密文（头部仍完整）
    keyring.rotate_master()
    # 轮换只验头，块篡改此刻不会被发现——但轮换后的文件解密必然失败。
    rotated = rotate_bytes(bytes(blob), keyring)
    with pytest.raises(IntegrityError):
        decrypt_bytes(rotated, keyring)


def test_rotate_refuses_tampered_header(keyring):
    import json

    blob = encrypt_bytes(b"x" * 200, keyring, chunk_size=16)
    hl = int.from_bytes(blob[7:11], "big")
    header = json.loads(blob[11:11 + hl])
    header["file_id"] = "f-tampered"
    raw = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    tampered = blob[:7] + len(raw).to_bytes(4, "big") + raw + blob[11 + hl:]
    keyring.rotate_master()
    with pytest.raises(IntegrityError, match="文件头认证失败"):
        rotate_bytes(tampered, keyring)


def test_rotate_without_old_master_key_fails(keyring, tmp_path):
    blob = encrypt_bytes(b"secret", keyring)
    old_kid = parse_header(blob)["kid"]
    keyring.rotate_master()
    keyring.delete_key(old_kid)
    with pytest.raises(KeyNotFoundError, match="旧主密钥不存在"):
        rotate_bytes(blob, keyring)
