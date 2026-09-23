"""Tests for package containment / zip-slip / zip-bomb defenses."""

import io
import os
import stat
import zipfile

import pytest

from provenance_service.package_safety import (
    MAX_TOTAL_UNCOMPRESSED,
    UnsafePackage,
    extract_zip_safe,
    normalize_member_path,
)


def _zip(entries, host_unix: bool = True, compression=zipfile.ZIP_STORED) -> bytes:
    """entries: [(name, data_bytes, unix_mode_or_None)]"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, data, mode in entries:
            zi = zipfile.ZipInfo(name)
            zi.compress_type = compression
            if mode is not None:
                # external_attr packs st_mode in its high 16 bits (host-agnostic;
                # the service checks the type bits without trusting host byte).
                zi.external_attr = mode << 16 if host_unix else 0
            zf.writestr(zi, data)
    return buf.getvalue()


@pytest.mark.parametrize("bad", [
    "../evil.sol",
    "a/../../evil.sol",
    "/abs/path.sol",
    "..\\..\\evil.bat",
    "C:/windows/x.sol",
    "a/../b/../../x",
    "a/\x00b",
    "",
    ".",
])
def test_normalize_rejects(bad):
    with pytest.raises(UnsafePackage):
        normalize_member_path(bad)


@pytest.mark.parametrize("good", [
    "a.sol", "src/a.sol", "a/b/c.sol", "./a.sol",
    "a/./b.sol", "a\\b\\c.sol",
])
def test_normalize_accepts(good):
    assert "/" in normalize_member_path(good) or normalize_member_path(good)


def test_safe_zip_roundtrip():
    data = _zip([("src/a.sol", b"aaa", None),
                 ("src/nested/b.sol", b"bbbb", 0o644),
                 ("root.txt", b"r", None)])
    files = extract_zip_safe(data)
    assert files["src/a.sol"] == b"aaa"
    assert files["src/nested/b.sol"] == b"bbbb"
    assert all(not p.startswith("/") and ".." not in p.split("/") for p in files)


@pytest.mark.parametrize("name", [
    "../evil.sol", "a/../../evil.sol", "/tmp/evil.sol", "..\\..\\evil.bat",
])
def test_zip_slip_rejected(name):
    with pytest.raises(UnsafePackage):
        extract_zip_safe(_zip([(name, b"x", None), ("ok.sol", b"y", None)]))


def test_symlink_member_rejected():
    data = _zip([("link.sol", b"../../etc/passwd",
                  stat.S_IFLNK | 0o777)])
    with pytest.raises(UnsafePackage, match="symlink"):
        extract_zip_safe(data)


def test_device_member_rejected():
    data = _zip([("fifo.sol", b"", stat.S_IFIFO | 0o644)])
    with pytest.raises(UnsafePackage):
        extract_zip_safe(data)


def test_nul_byte_member_rejected_even_when_zipfile_strips_name():
    # zipfile truncates member names at a NUL when writing, so take a valid
    # archive and overwrite the (equal-length) member name with one that
    # embeds a NUL byte, in both the local header and central directory.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("aaaaaaaa", b"x")  # 8-char name
    data = bytearray(buf.getvalue())
    poisoned = b"aaa\x00aaaa"
    assert len(poisoned) == len(b"aaaaaaaa")
    # overwrite local-header name
    local = data.find(b"PK\x03\x04")
    data[local + 30:local + 38] = poisoned
    # overwrite central-directory name
    cdr = data.find(b"PK\x01\x02")
    data[cdr + 46:cdr + 54] = poisoned
    with pytest.raises(UnsafePackage, match="NUL"):
        extract_zip_safe(bytes(data))


def test_duplicate_normalized_path_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("a/b.sol", b"1")
        zf.writestr("a/./b.sol", b"2")
    with pytest.raises(UnsafePackage, match="duplicate"):
        extract_zip_safe(buf.getvalue())


def test_empty_archive_rejected():
    with pytest.raises(UnsafePackage):
        extract_zip_safe(_zip([]))


def test_not_a_zip_rejected():
    with pytest.raises(UnsafePackage):
        extract_zip_safe(b"this is not a zip file at all")


def test_zip_bomb_uncompressed_size_rejected():
    # Several compressible members that individually fit the per-file limit
    # but together exceed the aggregate uncompressed budget.
    import os as _os
    block = _os.urandom(4096)
    chunk = (block * (15 * 1024 * 1024 // len(block) + 1))[:15 * 1024 * 1024]
    entries = [(f"part{i}.bin", chunk, None) for i in range(8)]  # 120 MiB total
    raw = _zip(entries, compression=zipfile.ZIP_DEFLATED)
    assert len(raw) < 25 * 1024 * 1024  # compressed gate passes
    with pytest.raises(UnsafePackage, match="uncompressed"):
        extract_zip_safe(raw)


def test_scripts_are_extracted_as_data_only(tmp_path):
    """The packaged build.sh is readable bytes but never executed by us.

    The marker file that script would create must never appear.
    """
    from provenance_service.fixtures import SHELL_SCRIPT, build_zip, example_sources

    marker = "/tmp/provenance_script_should_never_run.marker"
    if os.path.exists(marker):
        os.remove(marker)
    files = extract_zip_safe(build_zip(example_sources()))
    assert files["scripts/build.sh"] == SHELL_SCRIPT.encode()
    # The service performs no execution; a subprocess is deliberately not run.
    assert not os.path.exists(marker)
