"""Archive safety tests — traversal, symlinks, caps."""
from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from app.lockgate.archive import (
    MAX_MEMBERS,
    UnsafeArchiveError,
    parse_archive,
)


def _tar(members: dict[str, bytes | tuple[str, str]]) -> bytes:
    """members maps name -> bytes (regular file) or (kind, target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for name, val in members.items():
            if isinstance(val, tuple):
                kind, target = val
                info = tarfile.TarInfo(name)
                if kind == "symlink":
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                elif kind == "hardlink":
                    info.type = tarfile.LNKTYPE
                    info.linkname = target
                tf.addfile(info)
            else:
                info = tarfile.TarInfo(name)
                info.size = len(val)
                tf.addfile(info, io.BytesIO(val))
    return buf.getvalue()


def test_clean_tar_reads_in_memory():
    arch = parse_archive(_tar({"package.json": b"{}", "vendor/a.tgz": b"x"}))
    assert arch.get("package.json") == b"{}"


def test_dotdot_traversal_refused():
    with pytest.raises(UnsafeArchiveError, match="traversal"):
        parse_archive(_tar({"../evil": b"x"}))


def test_absolute_path_refused():
    with pytest.raises(UnsafeArchiveError, match="absolute"):
        parse_archive(_tar({"/etc/passwd": b"x"}))


def test_symlink_refused():
    with pytest.raises(UnsafeArchiveError, match="symlink|link"):
        parse_archive(_tar({"link": ("symlink", "/etc/passwd")}))


def test_hardlink_refused():
    with pytest.raises(UnsafeArchiveError, match="link"):
        parse_archive(_tar({"link": ("hardlink", "package.json")}))


def test_duplicate_entry_refused():
    # tarfile allows two members with the same name; we must reject
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for _ in range(2):
            info = tarfile.TarInfo("package.json")
            data = b"{}"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    with pytest.raises(UnsafeArchiveError, match="duplicate"):
        parse_archive(buf.getvalue())


def test_zip_traversal_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil", "x")
    with pytest.raises(UnsafeArchiveError):
        parse_archive(buf.getvalue())


def test_zip_symlink_mode_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zi = zipfile.ZipInfo("evil-link")
        zi.create_system = 3
        zi.external_attr = (0o120777 << 16)
        zf.writestr(zi, "target")
    with pytest.raises(UnsafeArchiveError, match="symlink"):
        parse_archive(buf.getvalue())


def test_too_many_members():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:") as tf:
        for i in range(MAX_MEMBERS + 1):
            info = tarfile.TarInfo(f"f{i}")
            info.size = 0
            tf.addfile(info)
    with pytest.raises(UnsafeArchiveError, match="too many"):
        parse_archive(buf.getvalue())


def test_garbage_rejected():
    with pytest.raises(UnsafeArchiveError, match="neither"):
        parse_archive(b"definitely not an archive" * 10)


def test_nothing_is_written_to_disk(tmp_path, monkeypatch):
    # A belt-and-braces check: parsing a bundle while the CWD points at a
    # pristine temp dir must not create files.
    monkeypatch.chdir(tmp_path)
    parse_archive(_tar({"package.json": b"{}"}))
    assert list(tmp_path.iterdir()) == []
