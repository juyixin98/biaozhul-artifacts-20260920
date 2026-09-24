"""Tests for hostile-archive handling."""

from __future__ import annotations

import io
import tarfile
import zipfile

import pytest

from app.extract import (
    BundleError,
    extract_bundle,
    safe_member_path,
    strip_common_root,
)


def _tar(files: dict[str, bytes], *, gz: bool = True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if gz else "w:") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _tar_raw(members: list[tarfile.TarInfo], mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for info, data in members:
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_plain_tar_gz_roundtrip_and_root_strip():
    blob = _tar(
        {
            "proj-abc/package.json": b"{}",
            "proj-abc/package-lock.json": b"{}",
        }
    )
    bundle = extract_bundle(blob)
    assert set(bundle.files) == {"package.json", "package-lock.json"}
    assert bundle.kind == "tar.gz"


def test_zip_roundtrip():
    blob = _zip({"package.json": b"{}", "vendor/x.tgz": b"abc"})
    bundle = extract_bundle(blob)
    assert bundle.files["vendor/x.tgz"] == b"abc"
    assert bundle.kind == "zip"


def test_rejects_unknown_magic():
    with pytest.raises(BundleError, match="unsupported archive format"):
        extract_bundle(b"not an archive at all")


@pytest.mark.parametrize(
    "evil",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "a/../../b",
        "..\\..\\windows\\system32",
        "C:/windows/system32",
        "//server/share",
    ],
)
def test_path_traversal_rejected(evil):
    with pytest.raises(BundleError):
        safe_member_path(evil)


def test_collapsed_slashes_normalize_safely():
    # a//../b normalizes to "b" and never escapes the root.
    assert safe_member_path("a//../b") == "b"


def test_symlink_tarball_rejected():
    target = tarfile.TarInfo("package.json")
    target.size = 2
    link = tarfile.TarInfo("link")
    link.type = tarfile.SYMTYPE
    link.linkname = "/etc/passwd"
    blob = _tar_raw([(target, b"{}"), (link, b"")])
    with pytest.raises(BundleError, match="symbolic/hard link"):
        extract_bundle(blob)


def test_zip_slip_rejected():
    blob = _zip({"../evil.txt": b"x", "package.json": b"{}"})
    with pytest.raises(BundleError, match="escapes"):
        extract_bundle(blob)


def test_zip_symlink_rejected():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zi = zipfile.ZipInfo("evil-link")
        zi.create_system = 3
        zi.external_attr = (0o120777 << 16) | 0o0777
        zf.writestr(zi, "/etc/passwd")
        zf.writestr("package.json", b"{}")
    with pytest.raises(BundleError, match="symbolic link"):
        extract_bundle(buf.getvalue())


def test_bomb_ratio_rejected():
    # Highly compressible payload > 200x compressed size.
    payload = b"\x00" * 5_000_000
    blob = _tar({"package.json": payload})
    import gzip

    compressed = len(gzip.compress(payload))
    if len(payload) > compressed * 200:
        with pytest.raises(BundleError, match="zip bomb"):
            extract_bundle(blob)
    else:  # pragma: no cover - environment dependent
        pytest.skip("compression ratio not extreme enough on this platform")


def test_common_root_logic():
    files = {"a/b.txt": b"1"}
    assert strip_common_root(files) == {"b.txt": b"1"}
    mixed = {"a/b.txt": b"1", "top.txt": b"2"}
    assert strip_common_root(mixed) == mixed
