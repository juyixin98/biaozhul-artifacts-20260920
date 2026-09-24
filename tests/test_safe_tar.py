"""核心安全解包测试：恶意路径、链接链、解压炸弹、原子性。"""

from __future__ import annotations

import gzip
import io
import os
import socket
import tarfile
from pathlib import Path

import pytest

from app.config import Settings
from app.errors import UnsafeArchiveError
from app.safe_tar import ArchiveReadError, safe_extract
from tests.conftest import tarinfo


def settings(tmp_path, **over) -> Settings:
    base = dict(
        max_upload_bytes=2 * 1024 * 1024,
        max_total_bytes=64 * 1024,
        max_file_bytes=48 * 1024,
        max_entries=100,
        max_symlink_hops=8,
        max_compression_ratio=100.0,
        data_dir=str(tmp_path),
    )
    base.update(over)
    return Settings(**base)


def run_extract(data: bytes, tmp_path: Path, st: Settings | None = None,
                compress_name: str = "none"):
    st = st or settings(tmp_path)
    root = tmp_path / "extracted"
    out = root
    return safe_extract(
        io.BytesIO(data),
        str(out),
        st,
        extraction_id="deadbeef" * 4,
        archive_sha256="0" * 64,
        archive_size=len(data),
        compression=compress_name,
    )


def expect_reject(data: bytes, tmp_path: Path, code: str,
                  st: Settings | None = None):
    with pytest.raises(UnsafeArchiveError) as ei:
        run_extract(data, tmp_path, st)
    assert ei.value.code == code, ei.value.detail
    return ei.value


# --------------------------------------------------------------------------- #
# 恶意路径
# --------------------------------------------------------------------------- #
def test_absolute_path_rejected(builder, tmp_path):
    b = builder().add(tarinfo("/etc/passwd_evil", "file", size=4), b"data")
    expect_reject(b.getvalue(), tmp_path, "absolute_path")


def test_dotdot_traversal_rejected(builder, tmp_path):
    b = builder().add(tarinfo("../../../tmp/evil.txt", "file", size=4), b"data")
    expect_reject(b.getvalue(), tmp_path, "path_traversal")


def test_embedded_dotdot_rejected(builder, tmp_path):
    b = builder().add(tarinfo("a/../../escape", "file", size=4), b"data")
    expect_reject(b.getvalue(), tmp_path, "path_traversal")


def test_backslash_absolute_no_escape(builder, tmp_path):
    # 前导反斜杠按 POSIX 当作绝对路径形态拒绝（跨平台防御）
    b = builder().add(tarinfo("\\..\\windows", "file", size=4), b"data")
    expect_reject(b.getvalue(), tmp_path, "absolute_path")


def test_nul_byte_rejected():
    # tarfile 写入时会在 NUL 处截断，故直接对名称规范化函数做单元测试
    from app.safe_tar import _normalize_member_name

    for bad in ("good\x00evil", "a/\x00b", "\x00"):
        with pytest.raises(UnsafeArchiveError) as ei:
            _normalize_member_name(bad)
        assert ei.value.code == "nul_in_name"
    assert _normalize_member_name("a/b") == "a/b"


# --------------------------------------------------------------------------- #
# 符号链接逃逸 / 链接链
# --------------------------------------------------------------------------- #
def test_symlink_to_absolute_rejected(builder, tmp_path):
    b = builder().add_symlink("lnk", "/etc")
    expect_reject(b.getvalue(), tmp_path, "link_escape")


def test_symlink_dotdot_then_file_rejected(builder, tmp_path):
    # lnk -> ../../.. 然后 lnk/payload.txt 会写到 root 之外
    b = (
        builder()
        .add_symlink("lnk", "../../..")
        .add_file("lnk/payload.txt", b"pwn")
    )
    expect_reject(b.getvalue(), tmp_path, "link_escape")


def test_symlink_self_loop_rejected(builder, tmp_path):
    b = builder().add_symlink("me", "me")
    expect_reject(b.getvalue(), tmp_path, "symlink_loop")


def test_symlink_mutual_loop_rejected(builder, tmp_path):
    b = builder().add_symlink("a", "b").add_symlink("b", "a")
    expect_reject(b.getvalue(), tmp_path, "symlink_loop")


def test_long_symlink_chain_rejected(builder, tmp_path):
    b = builder()
    for i in range(20):
        b.add_symlink(f"s{i}", f"s{i+1}")
    b.add_symlink("s20", "s0")
    expect_reject(b.getvalue(), tmp_path, "symlink_loop")


def test_hardlink_to_absolute_missing(builder, tmp_path):
    b = builder().add_hardlink("h", "/etc/passwd")
    expect_reject(b.getvalue(), tmp_path, "absolute_path")


def test_hardlink_dotdot_escape(builder, tmp_path):
    b = builder().add_hardlink("h", "../../../etc/passwd")
    expect_reject(b.getvalue(), tmp_path, "path_traversal")


def test_hardlink_chain_loop(builder, tmp_path):
    b = (
        builder()
        .add_hardlink("h1", "h2")
        .add_hardlink("h2", "h1")
    )
    expect_reject(b.getvalue(), tmp_path, "symlink_loop")


def test_hardlink_target_not_file(builder, tmp_path):
    b = builder().add_dir("d").add_hardlink("h", "d")
    expect_reject(b.getvalue(), tmp_path, "bad_link")


# --------------------------------------------------------------------------- #
# 危险条目类型
# --------------------------------------------------------------------------- #
def test_device_entry_rejected(builder, tmp_path):
    b = builder().add(tarinfo("dev", "char"))
    expect_reject(b.getvalue(), tmp_path, "unsupported_entry_type")


def test_fifo_entry_rejected(builder, tmp_path):
    b = builder().add(tarinfo("pipe", "fifo"))
    expect_reject(b.getvalue(), tmp_path, "unsupported_entry_type")


def test_setuid_bits_ignored(builder, tmp_path):
    b = builder().add_file("setuid.sh", b"echo hi", mode=0o4755)
    run_extract(b.getvalue(), tmp_path)
    st_mode = os.stat(
        tmp_path / "extracted" / ("deadbeef" * 4) / "setuid.sh"
    ).st_mode
    assert st_mode & 0o777 == 0o600
    assert not (st_mode & 0o4000)


# --------------------------------------------------------------------------- #
# 解压炸弹 / 配额
# --------------------------------------------------------------------------- #
def test_declared_total_over_quota_rejected(builder, tmp_path):
    st = settings(tmp_path, max_total_bytes=10)
    b = builder().add_file("big.bin", b"x" * 5000)
    expect_reject(b.getvalue(), tmp_path, "quota_exceeded", st)


def test_single_file_limit_rejected(builder, tmp_path):
    st = settings(tmp_path, max_file_bytes=100, max_total_bytes=10_000_000)
    b = builder().add_file("big.bin", b"x" * 5000)
    expect_reject(b.getvalue(), tmp_path, "file_too_large", st)


def test_too_many_entries_rejected(builder, tmp_path):
    st = settings(tmp_path, max_entries=3)
    b = builder()
    for i in range(5):
        b.add_file(f"f{i}", b"a")
    expect_reject(b.getvalue(), tmp_path, "too_many_entries", st)


def test_gzip_bomb_rejected(tmp_path):
    # 高压缩比的零数据：1MB 零压缩后仅约 1KB
    payload = b"\x00" * (1 * 1024 * 1024)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        m = tarinfo("zeros.bin", "file", size=len(payload))
        t.addfile(m, io.BytesIO(payload))
    gz = gzip.compress(raw.getvalue(), compresslevel=9)
    assert len(gz) < 5000
    st = settings(
        tmp_path,
        max_file_bytes=10 * 1024 * 1024,
        max_total_bytes=10 * 1024 * 1024,
        max_compression_ratio=50,
    )
    expect_reject(gz, tmp_path, "compression_bomb", st)


# --------------------------------------------------------------------------- #
# 原子性 / 隔离
# --------------------------------------------------------------------------- #
def test_failure_publishes_nothing(builder, tmp_path):
    root = tmp_path / "extracted"
    b = (
        builder()
        .add_file("ok.txt", b"ok")
        .add(tarinfo("../../../tmp/evil", "file", size=4), b"data")
    )
    with pytest.raises(UnsafeArchiveError):
        run_extract(b.getvalue(), tmp_path)
    # 正式目录不存在，暂存目录被清理
    assert not (root / ("deadbeef" * 4)).exists()
    leftovers = [p for p in root.glob(".staging-*")] if root.exists() else []
    assert leftovers == []


def test_no_write_outside_root(builder, tmp_path):
    """逃逸条目失败时，root 外（tmp_path 兄弟位置）不能出现任何文件。"""
    sentinel_parent = tmp_path / "outside"
    sentinel_parent.mkdir()
    root = tmp_path / "extracted"
    root.mkdir()

    evil_names = [
        "../outside/evil1",
        "../../" * 10 + f"evil2_{socket.gethostname()}",
    ]
    for evil in evil_names:
        b = builder().add(tarinfo(evil, "file", size=4), b"evil")
        with pytest.raises(UnsafeArchiveError):
            run_extract(b.getvalue(), tmp_path)
    assert list(sentinel_parent.iterdir()) == []


def test_successful_extract_layout_and_bytes(builder, tmp_path):
    b = (
        builder()
        .add_dir("dir")
        .add_file("dir/a.txt", b"hello")
        .add_file("dir/b.bin", b"\x00\x01\x02\x03")
        .add_symlink("dir/link", "a.txt")
        .add_hardlink("dir/hard", "dir/b.bin")
    )
    result = run_extract(b.getvalue(), tmp_path)
    pub = tmp_path / "extracted" / result.extraction_id
    assert (pub / "dir" / "a.txt").read_bytes() == b"hello"
    assert os.readlink(pub / "dir" / "link") == "a.txt"
    assert (pub / "dir" / "link").read_bytes() == b"hello"
    st_a = (pub / "dir" / "b.bin").stat()
    st_h = (pub / "dir" / "hard").stat()
    assert st_a.st_ino == st_h.st_ino  # 确为硬链接
    assert result.bytes_written == 9  # 按实际写出字节：5 + 4
    assert result.entry_count == 5
    assert os.stat(pub / "dir").st_mode & 0o777 == 0o700


def test_dangling_relative_symlink_allowed(builder, tmp_path):
    # 悬空但不逃逸的相对符号链接是合法的
    b = builder().add_dir("d").add_symlink("d/link", "not-yet.txt")
    result = run_extract(b.getvalue(), tmp_path)
    link = tmp_path / "extracted" / result.extraction_id / "d" / "link"
    assert link.is_symlink() and os.readlink(link) == "not-yet.txt"


def test_symlink_directory_alias(builder, tmp_path):
    # linkdir -> realdir，文件经 linkdir 写入时落点必须仍在 realdir（root 内）
    b = (
        builder()
        .add_dir("realdir")
        .add_symlink("linkdir", "realdir")
        .add_file("linkdir/inside.txt", b"ins")
    )
    result = run_extract(b.getvalue(), tmp_path)
    pub = tmp_path / "extracted" / result.extraction_id
    assert (pub / "realdir" / "inside.txt").read_bytes() == b"ins"


def test_benign_gzip_roundtrip(tarbuilder_factory, tmp_path):
    b = tarbuilder_factory("w:gz")
    b.add_file("hello.txt", b"gzip-ok")
    result = run_extract(b.getvalue(), tmp_path, compress_name="gzip")
    pub = tmp_path / "extracted" / result.extraction_id
    assert (pub / "hello.txt").read_bytes() == b"gzip-ok"
    assert result.compression == "gzip"


def test_broken_archive_rejected(tmp_path):
    with pytest.raises(ArchiveReadError):
        run_extract(b"this is not a tar at all" * 10, tmp_path)


def test_empty_archive_accepted_as_zero_entries(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w"):
        pass
    result = run_extract(buf.getvalue(), tmp_path)
    assert result.entry_count == 0
    assert (tmp_path / "extracted" / result.extraction_id).is_dir()
