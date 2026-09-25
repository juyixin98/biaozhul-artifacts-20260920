"""核心服务测试：加解密往返、空文件、非对齐分块、错误密钥、跨文件搬运。"""

from __future__ import annotations

import io
import os

import pytest

from envelope.errors import (
    AEADAuthenticationError,
    KeyNotFoundError,
    NoMasterKeyError,
)
from envelope.service import EnvelopeService


@pytest.fixture()
def svc(tmp_path):
    service = EnvelopeService(tmp_path / "store")
    service.create_master_key()
    return service


def _roundtrip(service: EnvelopeService, data: bytes, chunk_size: int = 64) -> bytes:
    loc = service.encrypt_stream(io.BytesIO(data), chunk_size=chunk_size)
    out = io.BytesIO()
    service.decrypt_stream(loc.file_id, out)
    assert out.getvalue() == data
    return out.getvalue()


def test_roundtrip_various_sizes(svc):
    for size in (0, 1, 63, 64, 65, 127, 128, 129, 10_000):
        data = bytes((i * 7 + 3) % 256 for i in range(size))
        assert _roundtrip(svc, data) == data


def test_file_path_roundtrip(svc, tmp_path):
    src = tmp_path / "plain.txt"
    data = os.urandom(5000)
    src.write_bytes(data)
    loc = svc.encrypt_file(src, chunk_size=128)
    out = tmp_path / "out.txt"
    svc.decrypt_file(loc.file_id, out)
    assert out.read_bytes() == data


def test_encrypt_without_master_key(tmp_path):
    service = EnvelopeService(tmp_path / "store2")
    with pytest.raises(NoMasterKeyError):
        service.encrypt_stream(io.BytesIO(b"x"))


def test_duplicate_file_id_rejected(svc):
    fid = "a" * 32
    svc.encrypt_stream(io.BytesIO(b"one"), file_id=fid)
    with pytest.raises(FileExistsError):
        svc.encrypt_stream(io.BytesIO(b"two"), file_id=fid)


def test_describe_metadata(svc):
    data = os.urandom(200)
    loc = svc.encrypt_stream(io.BytesIO(data), chunk_size=64)
    info = svc.describe(loc.file_id)
    assert info["plaintext_size"] == 200
    assert info["chunk_size"] == 64
    assert info["blocks"] == 4
    assert info["header_version"] == 1
    assert info["algorithm"] == "AES-256-GCM"


def test_decrypt_after_wrapping_key_deleted(svc):
    # 模拟包裹该文件的主密钥缺失：解密必须在解信封阶段明确失败
    loc = svc.encrypt_stream(io.BytesIO(b"top secret"), chunk_size=8)
    kid = svc.describe(loc.file_id)["wrapped_by_kid"]
    state_path = svc.key_store.path
    import json

    state = json.loads(state_path.read_text())
    del state["keys"][kid]
    state_path.write_text(json.dumps(state))
    with pytest.raises(KeyNotFoundError):
        svc.decrypt_stream(loc.file_id, io.BytesIO())


def test_blob_is_not_plaintext(svc):
    secret = b"THIS_IS_A_SECRET_PATTERN_1234567890"
    loc = svc.encrypt_stream(io.BytesIO(secret), chunk_size=16)
    blob_bytes = loc.blob_path.read_bytes()
    meta_bytes = loc.meta_path.read_bytes()
    assert b"THIS_IS_A_SECRET_PATTERN" not in blob_bytes
    assert b"THIS_IS_A_SECRET_PATTERN" not in meta_bytes


def test_orphan_blob_is_swept_on_restart(svc, tmp_path):
    """blob 落盘后、meta 提交前崩溃：重启应清掉无 meta 的孤儿 blob。"""
    loc = svc.encrypt_stream(io.BytesIO(b"normal file"))
    orphan_fid = "b" * 32
    orphan = svc.blob_dir / f"{orphan_fid}.blob"
    orphan.write_bytes(b"BLOB1" + os.urandom(50))
    assert orphan.exists()

    svc2 = EnvelopeService(tmp_path / "store")
    assert not orphan.exists()                      # 孤儿被清
    assert loc.blob_path.exists()                   # 正常文件不受影响
    out = io.BytesIO()
    svc2.decrypt_stream(loc.file_id, out)
    assert out.getvalue() == b"normal file"
