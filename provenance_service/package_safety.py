"""Safe extraction of uploaded source packages.

The service NEVER executes anything inside an uploaded package.  It only
reads it as an archive and hashes its member files.  Archives are still
untrusted input: zip-slip traversal, absolute paths, symlink/hardlink
members, device files and decompression bombs are rejected before any
member is written to or read from disk.  Extraction happens into an
in-memory mapping; nothing is materialized outside an explicit temp dir.
"""

from __future__ import annotations

import io
import posixpath
import zipfile

MAX_PACKAGE_BYTES = 25 * 1024 * 1024          # 25 MiB compressed upload
MAX_MEMBERS = 10_000
MAX_TOTAL_UNCOMPRESSED = 100 * 1024 * 1024    # 100 MiB zip-bomb guard
MAX_SINGLE_FILE = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100                   # compressed vs uncompressed guard


class UnsafePackage(ValueError):
    """Raised when an uploaded package violates the containment policy."""


def _scan_raw_names_for_nul(data: bytes) -> None:
    """Reject archives whose central-directory member names contain NULs.

    ``zipfile`` truncates member names at a NUL in its high-level API, so a
    raw scan of the central directory is needed to detect them.
    """
    pos = 0
    while True:
        idx = data.find(b"PK\x01\x02", pos)
        if idx < 0:
            return
        # Central directory fixed header is 46 bytes before variable fields.
        if idx + 46 <= len(data):
            nlen = int.from_bytes(data[idx + 28:idx + 30], "little")
            name = data[idx + 46:idx + 46 + nlen]
            if b"\x00" in name:
                raise UnsafePackage("NUL byte in archive member name")
        pos = idx + 4


def normalize_member_path(member: str) -> str:
    """Return a safe POSIX-style relative path or raise UnsafePackage.

    Backslashes are treated as separators (Windows-produced zips).  Drive
    letters, leading slashes, ``..`` segments and NUL bytes are rejected.
    """
    if "\x00" in member:
        raise UnsafePackage("NUL byte in archive member name")

    name = member.replace("\\", "/")
    if name.startswith("/"):
        raise UnsafePackage(f"absolute path in archive: {member!r}")

    # Windows drive prefix, e.g. C:/...
    if len(name) >= 2 and name[1] == ":":
        raise UnsafePackage(f"drive-qualified path in archive: {member!r}")

    parts: list[str] = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePackage(f"parent-directory traversal in archive: {member!r}")
        parts.append(part)
    if not parts:
        raise UnsafePackage(f"empty member name {member!r}")

    clean = posixpath.join(*parts)
    # Defense in depth: posixpath.normpath must not introduce '..'.
    if norm := posixpath.normpath(clean):
        if norm.startswith(".."):
            raise UnsafePackage(f"path escapes root after normalization: {member!r}")
    return clean


def extract_zip_safe(data: bytes) -> dict[str, bytes]:
    """Extract a zip archive into an in-memory mapping after validation.

    Returns ``{normalized_posix_path: file_bytes}``.  Directories and
    link/special members are not stored; any such special member is an
    outright rejection.
    """
    if len(data) > MAX_PACKAGE_BYTES:
        raise UnsafePackage(
            f"package too large: {len(data)} > {MAX_PACKAGE_BYTES} compressed bytes"
        )

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise UnsafePackage("not a valid zip archive") from exc

    # High-level zipfile strips member names at a NUL byte, which could hide a
    # malicious entry. Scan the raw central directory name records instead.
    _scan_raw_names_for_nul(data)

    members = zf.infolist()
    if len(members) > MAX_MEMBERS:
        raise UnsafePackage(f"too many archive members: {len(members)}")

    total = 0
    out: dict[str, bytes] = {}
    for info in members:
        # st_mode is packed in the high 16 bits of external_attr. The host
        # byte is not trustworthy (producers set it inconsistently, e.g. with
        # extra permission bits), so inspect the type bits whenever present.
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        if unix_mode:
            import stat as _stat

            fmt = unix_mode & 0o170000  # file-type bits
            if fmt == _stat.S_IFLNK:
                raise UnsafePackage(f"symlink members are forbidden: {info.filename!r}")
            # Plain permission bits without a type bit (common zip convention,
            # e.g. 0o644) are treated as a regular file; explicit special types
            # are rejected outright.
            if fmt in (_stat.S_IFCHR, _stat.S_IFBLK, _stat.S_IFIFO,
                       _stat.S_IFSOCK):
                raise UnsafePackage(
                    f"special (device/fifo/socket) member forbidden: {info.filename!r}"
                )

        # Validate the name itself before any read: zipfile can strip NULs
        # from member names, which would otherwise hide a malicious entry.
        if "\x00" in info.filename:
            raise UnsafePackage("NUL byte in archive member name")

        path = normalize_member_path(info.filename)
        if info.is_dir() or info.filename.endswith(("/", "\\")):
            continue
        if path in out:
            raise UnsafePackage(f"duplicate normalized path: {path}")
        if info.file_size > MAX_SINGLE_FILE:
            raise UnsafePackage(f"member too large: {path}")
        total += info.file_size
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise UnsafePackage("archive exceeds uncompressed size budget")
        out[path] = zf.read(info)

    if not out:
        raise UnsafePackage("archive contains no regular files")
    return out
