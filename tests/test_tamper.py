"""完整性测试（验收核心）。

覆盖：
1. 篡改单个数据块字节 → AEAD 认证失败；
2. 同一文件内交换两个块 → nonce/序号 与 AAD 绑定检测；
3. 跨文件交换块 → 文件 ID 绑定 + 不同 DEK 检测；
4. 截断密文（删尾帧 / 删半帧）→ 截断或块数不一致；
5. 篡改 / 替换 meta（kid、信封、文件 ID）→ AEAD 失败或一致性拒绝；
6. 追加伪造帧 → 块数不一致。
"""

from __future__ import annotations

import io
import os
import struct

import pytest

from envelope.errors import (
    AEADAuthenticationError,
    CorruptContainerError,
    InvalidFormatError,
    TruncatedContainerError,
)
from envelope.service import EnvelopeService

FRAME_HEADER = struct.Struct(">I")
MAGIC_LEN = 5


@pytest.fixture()
def svc(tmp_path):
    service = EnvelopeService(tmp_path / "store")
    service.create_master_key()
    return service


def _parse_frames(blob: bytes) -> list[tuple[int, int]]:
    """返回每帧的 (起始偏移, 帧长度)。"""
    assert blob[:MAGIC_LEN] == b"BLOB1"
    pos = MAGIC_LEN
    frames = []
    while pos < len(blob):
        (length,) = FRAME_HEADER.unpack(blob[pos : pos + 4])
        frames.append((pos, length))
        pos += 4 + length
    assert pos == len(blob)
    return frames


def _decrypt(service, fid) -> bytes:
    out = io.BytesIO()
    service.decrypt_stream(fid, out)
    return out.getvalue()


def test_tamper_ciphertext_byte_in_block(svc):
    data = os.urandom(300)
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=64)
    raw = bytearray(loc.blob_path.read_bytes())
    frames = _parse_frames(bytes(raw))
    # 翻转第二帧载荷中的一个密文字节（nonce 之后，tag 之前）
    offset, _length = frames[1]
    raw[offset + 4 + 12 + 5] ^= 0xFF
    loc.blob_path.write_bytes(bytes(raw))
    with pytest.raises(AEADAuthenticationError):
        _decrypt(svc, loc.file_id)


def test_tamper_tag_byte_in_block(svc):
    data = os.urandom(300)
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=64)
    raw = bytearray(loc.blob_path.read_bytes())
    frames = _parse_frames(bytes(raw))
    offset, length = frames[0]
    raw[offset + 4 + length - 1] ^= 0x01  # 翻转 tag 最后一个字节
    loc.blob_path.write_bytes(bytes(raw))
    with pytest.raises(AEADAuthenticationError):
        _decrypt(svc, loc.file_id)


def test_swap_blocks_within_same_file(svc):
    data = bytes(range(256)) * 4  # 1024 字节，64B/块 → 16 块
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=64)
    raw = bytearray(loc.blob_path.read_bytes())
    frames = _parse_frames(bytes(raw))

    # 把第 0 帧与第 3 帧的完整帧（含帧头）对调
    f0_off, f0_len = frames[0]
    f3_off, f3_len = frames[3]
    assert f0_len == f3_len
    frame0 = bytes(raw[f0_off : f0_off + 4 + f0_len])
    frame3 = bytes(raw[f3_off : f3_off + 4 + f3_len])
    raw[f3_off : f3_off + 4 + f3_len] = frame0
    raw[f0_off : f0_off + 4 + f0_len] = frame3
    loc.blob_path.write_bytes(bytes(raw))

    # 帧的 nonce 携带原块序号 → 第 0 帧位置出现序号 3 的 nonce，立即被拒
    with pytest.raises(AEADAuthenticationError):
        _decrypt(svc, loc.file_id)


def test_swap_payload_keeping_nonce(svc):
    """更强的攻击：交换两帧载荷但保留各位置的 nonce（只搬密文字节）。

    即便 nonce 不变，AAD 绑定的块序号与密文在原密钥下不再匹配，GCM tag 失败。
    """
    data = bytes(range(256)) * 4
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=64)
    raw = bytearray(loc.blob_path.read_bytes())
    frames = _parse_frames(bytes(raw))
    off0, len0 = frames[0]
    off1, len1 = frames[1]
    # sealed = nonce 之后的全部（密文+tag），两帧等长
    sealed0 = bytes(raw[off0 + 4 + 12 : off0 + 4 + len0])
    sealed1 = bytes(raw[off1 + 4 + 12 : off1 + 4 + len1])
    raw[off1 + 4 + 12 : off1 + 4 + len1] = sealed0
    raw[off0 + 4 + 12 : off0 + 4 + len0] = sealed1
    loc.blob_path.write_bytes(bytes(raw))
    with pytest.raises(AEADAuthenticationError):
        _decrypt(svc, loc.file_id)


def test_cross_file_block_swap(svc):
    loc_a = svc.encrypt_stream(io.BytesIO(os.urandom(300)), chunk_size=64)
    loc_b = svc.encrypt_stream(io.BytesIO(os.urandom(300)), chunk_size=64)

    raw_a = bytearray(loc_a.blob_path.read_bytes())
    raw_b = bytearray(loc_b.blob_path.read_bytes())
    fa = _parse_frames(bytes(raw_a))
    fb = _parse_frames(bytes(raw_b))

    # 用 B 的第 1 帧完整替换 A 的第 1 帧（帧等长）
    off_a, len_a = fa[1]
    off_b, len_b = fb[1]
    assert len_a == len_b
    raw_a[off_a : off_a + 4 + len_a] = bytes(raw_b[off_b : off_b + 4 + len_b])
    loc_a.blob_path.write_bytes(bytes(raw_a))

    # B 的帧 nonce 携带 B 内的序号（碰巧也是 1，序号相同！），
    # 但 DEK 不同、AAD 中的文件 ID 不同 → 认证失败
    with pytest.raises(AEADAuthenticationError):
        _decrypt(svc, loc_a.file_id)


def test_cross_file_meta_swap(svc):
    loc_a = svc.encrypt_stream(io.BytesIO(b"file-A-content"), chunk_size=8)
    loc_b = svc.encrypt_stream(io.BytesIO(b"file-B-content"), chunk_size=8)

    # 把 B 的 meta 复制成 A 的 meta（试图让 A 的 blob 配 B 的信封）
    loc_a.meta_path.write_bytes(loc_b.meta_path.read_bytes())
    # meta 外层 fid 与请求 / 文件名不一致 → 直接拒绝
    with pytest.raises(AEADAuthenticationError):
        svc.describe(loc_a.file_id)

    # 即使把文件整套（meta+blob）改名也一样：meta 内部 fid 校验不通过
    import shutil

    shutil.copy(loc_b.meta_path, loc_a.meta_path)
    shutil.copy(loc_b.blob_path, loc_a.blob_path)
    with pytest.raises(AEADAuthenticationError):
        _decrypt(svc, loc_a.file_id)


def test_truncate_last_frame_body(svc):
    loc = svc.encrypt_stream(io.BytesIO(os.urandom(300)), chunk_size=64)
    raw = loc.blob_path.read_bytes()
    frames = _parse_frames(raw)
    last_off, last_len = frames[-1]
    # 在最后一帧载荷中间截断（帧头声明 4+last_len，实际文件被切短）
    loc.blob_path.write_bytes(raw[: last_off + 4 + last_len // 2])
    with pytest.raises((TruncatedContainerError, AEADAuthenticationError)):
        _decrypt(svc, loc.file_id)


def test_truncate_drop_whole_frames(svc):
    loc = svc.encrypt_stream(io.BytesIO(os.urandom(300)), chunk_size=64)
    raw = loc.blob_path.read_bytes()
    frames = _parse_frames(raw)
    # 删掉最后两个完整帧：容器结构仍然"干净"，但块数与受保护头不符
    cut = frames[-2][0]
    loc.blob_path.write_bytes(raw[:cut])
    with pytest.raises(CorruptContainerError):
        _decrypt(svc, loc.file_id)


def test_truncate_inside_frame_header(svc):
    loc = svc.encrypt_stream(io.BytesIO(os.urandom(300)), chunk_size=64)
    raw = loc.blob_path.read_bytes()
    frames = _parse_frames(raw)
    last_off = frames[-1][0]
    loc.blob_path.write_bytes(raw[: last_off + 2])  # 帧头只剩 2 字节
    with pytest.raises(TruncatedContainerError):
        _decrypt(svc, loc.file_id)


def test_append_garbage_frame(svc):
    loc = svc.encrypt_stream(io.BytesIO(os.urandom(100)), chunk_size=64)
    raw = bytearray(loc.blob_path.read_bytes())
    raw += FRAME_HEADER.pack(12 + 16) + b"\x00" * (12 + 16)
    loc.blob_path.write_bytes(bytes(raw))
    # 多出一帧：要么 nonce 序号不匹配认证失败，要么块数不一致
    with pytest.raises((AEADAuthenticationError, CorruptContainerError)):
        _decrypt(svc, loc.file_id)


def test_tamper_magic(svc):
    loc = svc.encrypt_stream(io.BytesIO(b"hello"), chunk_size=8)
    raw = bytearray(loc.blob_path.read_bytes())
    raw[0] = ord("X")
    loc.blob_path.write_bytes(bytes(raw))
    with pytest.raises(InvalidFormatError):
        _decrypt(svc, loc.file_id)


def test_tamper_meta_envelope_bytes(svc):
    import json

    loc = svc.encrypt_stream(io.BytesIO(b"hello world!"), chunk_size=8)
    raw = bytearray(loc.meta_path.read_bytes())
    (outer_len,) = struct.unpack(">I", raw[5:9])
    outer = json.loads(raw[9 : 9 + outer_len])
    env = outer["envelope_b64"]
    # 定位到 envelope_b64 字段值中间（受 MK 的 GCM tag 保护），翻转一个 base64 字符
    idx = raw.index(env.encode()) + len(env) // 2
    raw[idx] ^= 0x01
    loc.meta_path.write_bytes(bytes(raw))
    # base64 字母表中翻转 1 位可能仍是合法 base64，此时 AEAD 必然失败；
    # 若碰巧破坏了 base64 编码，则在格式解析阶段被拒——两者都是安全拒绝。
    from envelope.errors import KeyNotFoundError

    with pytest.raises((AEADAuthenticationError, InvalidFormatError, KeyNotFoundError)):
        _decrypt(svc, loc.file_id)


def test_tamper_meta_kid(svc, tmp_path):
    import json

    loc = svc.encrypt_stream(io.BytesIO(b"hello world!"), chunk_size=8)
    # 正确解析外层 JSON、把 kid 改成另一把密钥，再原样写回
    second = svc.create_master_key()
    raw = loc.meta_path.read_bytes()
    (outer_len,) = FRAME_HEADER.unpack(raw[5:9])
    outer = json.loads(raw[9 : 9 + outer_len])
    outer["kid"] = second.kid
    new_outer = json.dumps(outer, sort_keys=True, separators=(",", ":")).encode()
    rebuilt = raw[:5] + FRAME_HEADER.pack(len(new_outer)) + new_outer + raw[9 + outer_len :]
    # 长度可能变化，直接重写尾部长度标记为原值（header_ct 未变）
    loc.meta_path.write_bytes(rebuilt)
    # 用错误的主密钥解信封 → 认证失败
    with pytest.raises(AEADAuthenticationError):
        _decrypt(svc, loc.file_id)


def test_plaintext_size_declared_in_protected_header_is_authenticated(svc):
    # 受保护头里的规模字段由 DEK 的 GCM tag 保护，
    # 这里通过"截断 blob"间接证明：声明 blocks 与实际块数对不上会被拒。
    loc = svc.encrypt_stream(io.BytesIO(b"abcdefgh"), chunk_size=4)
    raw = loc.blob_path.read_bytes()
    frames = _parse_frames(raw)
    loc.blob_path.write_bytes(raw[: frames[-1][0]])  # 砍掉最后一块
    with pytest.raises(CorruptContainerError):
        _decrypt(svc, loc.file_id)
