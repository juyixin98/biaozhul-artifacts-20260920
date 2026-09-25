"""明文临时文件处理测试。"""

from __future__ import annotations

import io
import os
import stat

import pytest

from envelope.errors import AEADAuthenticationError
from envelope.securetemp import plaintext_temp_file, secure_unlink
from envelope.service import EnvelopeService


@pytest.fixture()
def svc(tmp_path):
    service = EnvelopeService(tmp_path / "store")
    service.create_master_key()
    return service


def test_no_plaintext_temp_left_after_failed_decrypt(svc, tmp_path):
    """解密到磁盘的过程中若认证失败，临时明文必须被安全删除。"""
    loc = svc.encrypt_stream(io.BytesIO(os.urandom(300)), chunk_size=64)
    out = tmp_path / "recovered.bin"

    # 篡改 blob 触发解密失败
    raw = bytearray(loc.blob_path.read_bytes())
    raw[20] ^= 0x01
    loc.blob_path.write_bytes(bytes(raw))

    with pytest.raises(AEADAuthenticationError):
        svc.decrypt_file(loc.file_id, out)

    assert not out.exists()
    leftovers = [
        p.name
        for p in out.parent.iterdir()
        if p.name.endswith(".tmp") or p.name.startswith(".plain.")
    ]
    assert leftovers == []


def test_temp_file_permissions(svc, tmp_path):
    loc = svc.encrypt_stream(io.BytesIO(b"secret"), chunk_size=4)
    out = tmp_path / "ok.bin"
    svc.decrypt_file(loc.file_id, out)
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_secure_unlink_removes_file(tmp_path):
    p = tmp_path / "secret.tmp"
    p.write_bytes(b"x" * 100)
    secure_unlink(p, size=100)
    assert not p.exists()


def test_plaintext_temp_context_cleans_up_on_exception(tmp_path):
    target = tmp_path / "final.bin"
    with pytest.raises(RuntimeError):
        with plaintext_temp_file(tmp_path) as (fh, commit):
            fh.write(b"plaintext that should vanish")
            fh.flush()
            tmp_name = fh.name
            raise RuntimeError("boom")
    assert not os.path.exists(tmp_name)
    assert not target.exists()


def test_plaintext_temp_context_commits(tmp_path):
    target = tmp_path / "final.bin"
    with plaintext_temp_file(tmp_path) as (fh, commit):
        fh.write(b"ok")
        commit(target)
    assert target.read_bytes() == b"ok"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
