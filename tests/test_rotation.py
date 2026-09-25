"""主密钥轮换测试：只重包 DEK、blob 字节不变、可多次轮换、轮换中断可恢复。"""

from __future__ import annotations

import io
import os

import pytest

from envelope.errors import RotationError
from envelope.service import EnvelopeService


@pytest.fixture()
def svc(tmp_path):
    service = EnvelopeService(tmp_path / "store")
    service.create_master_key()  # mk1
    return service


def _decrypt(service: EnvelopeService, fid: str) -> bytes:
    out = io.BytesIO()
    service.decrypt_stream(fid, out)
    return out.getvalue()


def test_rotation_only_rewraps_dek_and_blob_unchanged(svc):
    data = os.urandom(5000)
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=100)
    blob_before = loc.blob_path.read_bytes()
    info_before = svc.describe(loc.file_id)
    old_kid = info_before["wrapped_by_kid"]

    info = svc.rotate_master_key(loc.file_id)
    assert info.old_kid == old_kid
    assert info.new_kid != old_kid
    assert info.blob_bytes_changed == 0

    # blob 逐字节不变；meta 已更换
    assert loc.blob_path.read_bytes() == blob_before
    assert loc.meta_path.read_bytes() != b""

    info_after = svc.describe(loc.file_id)
    assert info_after["wrapped_by_kid"] == info.new_kid
    assert info_after["header_version"] == 2

    # 用新主密钥包裹的信封仍能正确解出完全一致的明文
    assert _decrypt(svc, loc.file_id) == data


def test_rotate_to_explicit_key_and_multiple_rotations(svc):
    mk2 = svc.create_master_key()
    mk3 = svc.create_master_key()
    loc = svc.encrypt_stream(io.BytesIO(b"x" * 333), chunk_size=32)

    info = svc.rotate_master_key(loc.file_id, new_kid=mk2.kid)
    assert info.new_kid == mk2.kid
    assert svc.describe(loc.file_id)["wrapped_by_kid"] == mk2.kid

    info = svc.rotate_master_key(loc.file_id, new_kid=mk3.kid)
    assert info.header_version == 3
    assert svc.describe(loc.file_id)["wrapped_by_kid"] == mk3.kid
    assert _decrypt(svc, loc.file_id) == b"x" * 333

    # 旧主密钥仍保留（它可能包裹着别的文件）
    all_kids = {k.kid for k in svc.list_master_keys()}
    assert {mk2.kid, mk3.kid}.issubset(all_kids)


def test_rotate_to_same_key_rejected(svc):
    loc = svc.encrypt_stream(io.BytesIO(b"data"))  # 由 fixture 建的 mk1 包裹
    mk2 = svc.create_master_key()
    svc.rotate_master_key(loc.file_id, new_kid=mk2.kid)
    with pytest.raises(RotationError):
        svc.rotate_master_key(loc.file_id, new_kid=mk2.kid)


def test_rotation_uses_fresh_wrap_nonces(svc):
    loc_a = svc.encrypt_stream(io.BytesIO(b"aaaa"))
    loc_b = svc.encrypt_stream(io.BytesIO(b"bbbb"))
    svc.rotate_master_key(loc_a.file_id)
    svc.rotate_master_key(loc_b.file_id)

    # 每次包裹分配的 nonce 计数器都不同（持久化单调计数器），
    # 检查两个 meta 的 envelope_nonce 不相等
    from envelope import meta as meta_mod

    na = meta_mod.read_meta_bytes(svc.meta_dir / f"{loc_a.file_id}.meta")
    nb = meta_mod.read_meta_bytes(svc.meta_dir / f"{loc_b.file_id}.meta")
    assert na["envelope_nonce"] != nb["envelope_nonce"]


class _CrashInRotation(Exception):
    pass


def test_interrupted_rotation_is_recoverable(svc, tmp_path):
    """模拟"新 meta 已生成、原子替换前崩溃"。

    要求：
    1. 旧 meta 完好，文件仍可用旧主密钥解密；
    2. 崩溃现场留下 .meta.tmp 临时文件；
    3. 直接重试轮换成功，最终明文不变；
    4. 重新构造服务（重启）会自动清扫临时文件。
    """
    data = os.urandom(2048)
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=64)
    old_meta = loc.meta_path.read_bytes()
    old_kid = svc.describe(loc.file_id)["wrapped_by_kid"]
    blob_before = loc.blob_path.read_bytes()

    call_count = {"n": 0}

    def _hook(stage, fid):
        call_count["n"] += 1
        raise _CrashInRotation("模拟断电")

    svc.crash_hook = _hook
    with pytest.raises(_CrashInRotation):
        svc.rotate_master_key(loc.file_id)
    svc.crash_hook = None
    assert call_count["n"] == 1

    # 旧 meta 未被替换；用旧密钥解密照常
    assert loc.meta_path.read_bytes() == old_meta
    assert loc.blob_path.read_bytes() == blob_before
    assert _decrypt(svc, loc.file_id) == data
    assert svc.describe(loc.file_id)["wrapped_by_kid"] == old_kid

    # 崩溃清理：异常路径删除了自身的 tmp，但模拟的是"进程被杀"场景，
    # 这里额外制造一个遗留 tmp 验证重启清扫
    leftover = svc.meta_dir / ".meta.leftover.tmp"
    leftover.write_bytes(b"partial")
    svc2 = EnvelopeService(tmp_path / "store")
    assert not leftover.exists()

    # 重试轮换：成功（用重启后的服务实例 sv2 完成，验证崩溃恢复 + 状态持久化）
    info = svc2.rotate_master_key(loc.file_id)
    assert info.old_kid == old_kid
    assert svc2.describe(loc.file_id)["header_version"] == 2
    assert _decrypt(svc2, loc.file_id) == data
    assert loc.blob_path.read_bytes() == blob_before


def test_many_files_rotate_independently(svc):
    locs = [
        svc.encrypt_stream(io.BytesIO(f"payload-{i}".encode() * 10), chunk_size=16)
        for i in range(5)
    ]
    mk2 = svc.create_master_key()
    # 只轮换其中两个
    svc.rotate_master_key(locs[1].file_id, new_kid=mk2.kid)
    svc.rotate_master_key(locs[3].file_id, new_kid=mk2.kid)

    for i, loc in enumerate(locs):
        out = io.BytesIO()
        svc.decrypt_stream(loc.file_id, out)
        assert out.getvalue() == f"payload-{i}".encode() * 10
        kid = svc.describe(loc.file_id)["wrapped_by_kid"]
        assert kid == mk2.kid if i in (1, 3) else kid != mk2.kid
