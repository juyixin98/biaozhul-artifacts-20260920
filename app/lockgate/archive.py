"""Safe, in-memory archive extraction.

The service never writes uploaded payloads to disk and never executes
anything contained in them. Both tar and zip inputs are validated against:

* absolute paths / drive letters,
* ``..`` traversal segments,
* symlink/hardlink/device members (tar) — refused outright,
* member-count and total-uncompressed-size caps,
* duplicate member entries.
"""
from __future__ import annotations

import io
import os
import tarfile
import zipfile
from dataclasses import dataclass

MAX_MEMBERS = 4_000
MAX_TOTAL_BYTES = 64 * 1024 * 1024  # 64 MiB of uncompressed input total
MAX_SINGLE_BYTES = 16 * 1024 * 1024


class UnsafeArchiveError(ValueError):
    pass


@dataclass
class Archive:
    # normalized path -> bytes (directories omitted)
    files: dict[str, bytes]

    def get(self, path: str) -> bytes | None:
        return self.files.get(_normalize(path))


def _normalize(member: str) -> str:
    p = member.replace("\\", "/")
    # strip a leading "./" (possibly repeated), but never "../"
    while p.startswith("./"):
        p = p[2:]
    # collapse repeated slashes
    while "//" in p:
        p = p.replace("//", "/")
    parts = []
    for part in p.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafeArchiveError(f"path traversal segment in archive entry: {member!r}")
        parts.append(part)
    return "/".join(parts)


def _check_name(name: str) -> str:
    if not name:
        raise UnsafeArchiveError("empty archive entry name")
    if name.startswith("/") or name.startswith("\\"):
        raise UnsafeArchiveError(f"absolute path in archive entry: {name!r}")
    if len(name) >= 256:
        raise UnsafeArchiveError(f"archive entry name too long: {name[:40]!r}...")
    return _normalize(name)


def parse_archive(data: bytes) -> Archive:
    """Parse a .tar/.tar.gz/.tgz or .zip payload entirely in memory."""
    if tarfile.is_tarfile(io.BytesIO(data)):
        return _parse_tar(data)
    if zipfile.is_zipfile(io.BytesIO(data)):
        return _parse_zip(data)
    raise UnsafeArchiveError(
        "payload is neither a valid tar (.tar/.tar.gz/.tgz) nor a zip archive"
    )


def _parse_tar(data: bytes) -> Archive:
    files: dict[str, bytes] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        members = tf.getmembers()
        if len(members) > MAX_MEMBERS:
            raise UnsafeArchiveError(f"too many archive entries: {len(members)}")
        for m in members:
            if not (m.isfile() or m.isdir()):
                # symlinks, hardlinks, devices, fifas are never accepted:
                # they are an installation-time escape hatch.
                raise UnsafeArchiveError(
                    f"unsupported tar entry type for {m.name!r} "
                    f"(symlinks/hardlinks/devices are refused)"
                )
            name = _check_name(m.name)
            if m.isdir():
                continue
            if name in files:
                raise UnsafeArchiveError(f"duplicate archive entry: {name!r}")
            if m.size > MAX_SINGLE_BYTES:
                raise UnsafeArchiveError(f"member too large: {name!r} ({m.size} bytes)")
            total += m.size
            if total > MAX_TOTAL_BYTES:
                raise UnsafeArchiveError("archive uncompressed size exceeds cap")
            src = tf.extractfile(m)
            payload = src.read() if src else b""
            files[name] = payload
    return Archive(files=files)


def _parse_zip(data: bytes) -> Archive:
    files: dict[str, bytes] = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise UnsafeArchiveError(f"too many archive entries: {len(infos)}")
        for info in infos:
            # Zip external_attr mode check for symlinks (Unix mode 0o120000)
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise UnsafeArchiveError(
                    f"symlink entry refused in zip: {info.filename!r}"
                )
            name = _check_name(info.filename)
            if info.is_dir():
                continue
            if name in files:
                raise UnsafeArchiveError(f"duplicate archive entry: {name!r}")
            if info.file_size > MAX_SINGLE_BYTES:
                raise UnsafeArchiveError(f"member too large: {name!r}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise UnsafeArchiveError("archive uncompressed size exceeds cap")
            files[name] = zf.read(info)
    return Archive(files=files)
