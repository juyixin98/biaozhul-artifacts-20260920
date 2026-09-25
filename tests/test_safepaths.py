"""路径安全测试: 词法逃逸、符号链接逃逸、特殊文件等。"""

import os
from pathlib import Path

import pytest

from sam.errors import UnsafePathError, UnsupportedFileTypeError
from sam.safepaths import iter_artifact_files, resolve_within, validate_relative


@pytest.mark.parametrize(
    "rel",
    [
        "../etc/passwd",
        "foo/../../etc/passwd",
        "foo/../bar/../../escape",
        "/etc/passwd",
        "//server/share",
        "foo/",
        "foo//bar",
        "./foo",
        "foo/./bar",
        "foo/bar/..",
        "..",
        ".",
        "",
        "C:foo",
        "C:\\foo",
        "foo\\bar",
        "foo\x00bar",
        "foo /",
        "foo./bar",
    ],
)
def test_lexical_traversal_rejected(rel):
    with pytest.raises(UnsafePathError):
        validate_relative(rel)


@pytest.mark.parametrize(
    "rel",
    [
        "a.txt",
        "bin/app.sh",
        "a/b/c/d",
        "report-2026_01.md",
        "目录/文件.txt",
        "x.y.z/ok-name_1",
    ],
)
def test_clean_relative_accepted(rel):
    assert validate_relative(rel) == rel


def test_symlink_escape_blocked(tmp_path: Path):
    root = tmp_path / "art"
    root.mkdir()
    (root / "real.txt").write_text("ok")
    outside = tmp_path / "secret.txt"
    outside.write_text("SECRET")

    # 符号链接指向根外文件: 枚举时 resolve 包含检查应拒绝
    os.symlink(outside, root / "evil-link")
    with pytest.raises(UnsafePathError):
        iter_artifact_files(root)

    # 直接对该相对路径做 resolve_within 也必须拒绝
    os.remove(root / "evil-link")
    os.symlink(outside, root / "evil-link")
    with pytest.raises(UnsafePathError):
        resolve_within(root, "evil-link")


def test_symlink_to_outside_directory_rejected(tmp_path: Path):
    root = tmp_path / "art"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x").write_text("x")
    os.symlink(outside, root / "linkdir")
    with pytest.raises((UnsafePathError, UnsupportedFileTypeError)):
        iter_artifact_files(root)


def test_internal_symlink_allowed(tmp_path: Path):
    root = tmp_path / "art"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (sub / "target.txt").write_text("hi")
    os.symlink(sub / "target.txt", root / "alias.txt")
    files = iter_artifact_files(root)
    assert files == ["alias.txt", "sub/target.txt"]


def test_enumeration_sorted_and_contained(tmp_path: Path):
    root = tmp_path / "art"
    root.mkdir()
    (root / "z").mkdir()
    (root / "a").mkdir()
    (root / "z" / "z2").write_text("1")
    (root / "a" / "a2").write_text("2")
    (root / "top").write_text("3")
    assert iter_artifact_files(root) == ["a/a2", "top", "z/z2"]


def test_fifo_rejected(tmp_path: Path):
    root = tmp_path / "art"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(UnsupportedFileTypeError):
        iter_artifact_files(root)


def test_resolve_within_nested_traversal(tmp_path: Path):
    # 即使词法层漏掉, 解析层也不得越界 (双重防护演示)
    root = (tmp_path / "art").resolve()
    root.mkdir()
    # 手工拼一个带 .. 的路径 —— validate_relative 先行拦截
    with pytest.raises(UnsafePathError):
        resolve_within(root, "a/../../outside")
