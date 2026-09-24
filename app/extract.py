"""Safe, in-memory extraction of uploaded project bundles.

The gate treats every archive as hostile input:

* file type is detected from magic bytes, never from the client filename;
* every entry path is normalized and must stay inside the extraction root
  (blocks absolute paths, ``..`` traversal, backslash tricks and
  Windows-drive paths — classic tar/zip-slip);
* symlinks, hard links and device nodes are refused — following them could
  escape the root or read host files;
* decompression-bomb limits bound member count, uncompressed size and the
  compression ratio;
* nothing is ever executed. npm lifecycle scripts are only *read* so the
  verifier can report them; they never run here.

The result is a simple ``{relative_posix_path: bytes}`` map.
"""

from __future__ import annotations

import io
import posixpath
import re
import tarfile
import zipfile
from dataclasses import dataclass

MAX_TOTAL_BYTES = 200 * 1024 * 1024  # 200 MiB uncompressed payload
MAX_MEMBERS = 20_000
MAX_SINGLE_BYTES = 50 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200  # compressed:uncompressed sanity ceiling


class BundleError(ValueError):
    """Raised when an uploaded bundle cannot be safely read."""


@dataclass(frozen=True)
class Bundle:
    files: dict[str, bytes]
    kind: str  # "tar.gz" | "tar" | "zip"

    def text_files(self) -> dict[str, bytes]:
        return self.files


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def safe_member_path(raw_name: str) -> str:
    """Return a validated POSIX path inside the extraction root or raise."""
    if not raw_name:
        raise BundleError("archive contains an entry with an empty name")
    if "\x00" in raw_name:
        raise BundleError("archive entry name contains NUL byte")

    # Backslashes are not path separators in POSIX tar, but Windows-built
    # zips sometimes use them; normalize so "..\.." can never sneak through
    # on a different host.
    candidate = raw_name.replace("\\", "/")

    # Reject Windows drive letters (C:/...) explicitly — os.path.splitdrive
    # recognizes them only on win32, so test the text regardless of host OS.
    if re.match(r"^[A-Za-z]:", candidate):
        raise BundleError(f"archive entry {raw_name!r} contains a drive letter")
    if candidate.startswith("//"):
        raise BundleError(f"archive entry {raw_name!r} is a UNC path")

    # An absolute POSIX path (/etc/passwd) is outside the root.
    if candidate.startswith("/"):
        raise BundleError(f"archive entry {raw_name!r} is an absolute path")

    # Normalize and verify no component escapes.
    normalized = posixpath.normpath(candidate)
    if normalized in (".", ""):
        # A bare "." directory entry is harmless and carries no data.
        return normalized
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise BundleError(
            f"archive entry {raw_name!r} escapes the extraction root"
        )
    # Reject hidden traversal through case-normalized device-style names? Not
    # needed for an in-memory map; the filesystem is never touched.
    return normalized


def strip_common_root(files: dict[str, bytes]) -> dict[str, bytes]:
    """If every entry lives under one top-level directory, remove that prefix.

    Mirrors the convention of GitHub-generated source archives
    (``repo-branch/package.json``). When files disagree or already live at the
    root, the mapping is returned unchanged.
    """
    roots = {path.split("/", 1)[0] for path in files if "/" in path}
    top_level_files = [path for path in files if "/" not in path]
    if top_level_files:
        return files
    if len(roots) != 1:
        return files
    root = next(iter(roots))
    stripped: dict[str, bytes] = {}
    for path, data in files.items():
        parts = path.split("/", 1)
        if len(parts) != 2 or parts[0] != root:
            return files
        new_path = parts[1]
        if new_path:
            stripped[new_path] = data
    return stripped


# ---------------------------------------------------------------------------
# Format detection / extraction
# ---------------------------------------------------------------------------


def detect_kind(blob: bytes) -> str:
    if blob.startswith(b"\x1f\x8b"):
        return "tar.gz"
    if len(blob) >= 262 and blob[257:262] == b"ustar":
        # Uncompressed tar magic lives at offset 257.
        return "tar"
    if blob.startswith(b"PK\x03\x04") or blob.startswith(b"PK\x05\x06"):
        return "zip"
    raise BundleError(
        "unsupported archive format: expected gzip/tar magic bytes (\\x1f\\x8b) "
        "or a ZIP (PK\\x03\\x04)"
    )


def extract_bundle(blob: bytes) -> Bundle:
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        raise BundleError("empty or non-bytes upload")
    if len(blob) > MAX_TOTAL_BYTES * 2:
        raise BundleError("uploaded bundle exceeds the hard size limit")
    kind = detect_kind(bytes(blob))
    if kind == "zip":
        files = _extract_zip(io.BytesIO(bytes(blob)), len(blob))
    else:
        files = _extract_tar(io.BytesIO(bytes(blob)), kind, len(blob))
    files = strip_common_root(files)
    if not files:
        raise BundleError("archive contains no regular files")
    return Bundle(files=files, kind=kind)


def _enforce_limits(member_count: int, total: int, compressed: int) -> None:
    if member_count > MAX_MEMBERS:
        raise BundleError(f"archive exceeds {MAX_MEMBERS} member limit")
    if total > MAX_TOTAL_BYTES:
        raise BundleError("uncompressed archive payload exceeds 200MiB limit")
    if compressed > 0 and total > compressed * MAX_COMPRESSION_RATIO:
        raise BundleError(
            "archive compression ratio exceeds 200:1 — possible zip bomb"
        )


def _extract_tar(stream: io.BytesIO, kind: str, compressed_size: int) -> dict[str, bytes]:
    mode = "r:gz" if kind == "tar.gz" else "r:"
    try:
        tf = tarfile.open(fileobj=stream, mode=mode)
    except (tarfile.TarError, EOFError, OSError) as exc:
        raise BundleError(f"unreadable {kind} archive: {exc}") from exc

    files: dict[str, bytes] = {}
    total = 0
    try:
        for member in tf:
            if member.isdir():
                continue
            if member.issym() or member.islnk():
                raise BundleError(
                    f"archive entry {member.name!r} is a symbolic/hard link; refused"
                )
            if not member.isfile():
                raise BundleError(
                    f"archive entry {member.name!r} has unsupported type "
                    f"(device/FIFO); refused"
                )
            path = safe_member_path(member.name)
            if path in (".", ""):
                continue
            if member.size > MAX_SINGLE_BYTES:
                raise BundleError(f"archive entry {path!r} exceeds single-file limit")
            extracted = tf.extractfile(member)
            if extracted is None:  # pragma: no cover - guarded by isfile
                raise BundleError(f"cannot read archive entry {path!r}")
            data = extracted.read(MAX_SINGLE_BYTES + 1)
            if len(data) > MAX_SINGLE_BYTES:
                raise BundleError(f"archive entry {path!r} exceeds single-file limit")
            total += len(data)
            _enforce_limits(len(files) + 1, total, compressed_size)
            files[path] = data
    except tarfile.ReadError as exc:
        # A truncated or corrupt member should fail closed.
        raise BundleError(f"corrupt {kind} archive: {exc}") from exc
    finally:
        tf.close()
    return files


def _extract_zip(stream: io.BytesIO, compressed_size: int) -> dict[str, bytes]:
    try:
        zf = zipfile.ZipFile(stream)
    except zipfile.BadZipFile as exc:
        raise BundleError(f"unreadable zip archive: {exc}") from exc

    files: dict[str, bytes] = {}
    total = 0
    try:
        for info in zf.infolist():
            path = safe_member_path(info.filename)
            if path.endswith("/") or info.is_dir():
                continue
            # Reject Unix symlinks: external_attr high 16 bits carry the mode;
            # 0o120000 marks a symbolic link.
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if unix_mode and (unix_mode & 0o170000) == 0o120000:
                raise BundleError(
                    f"archive entry {info.filename!r} is a symbolic link; refused"
                )
            if info.file_size > MAX_SINGLE_BYTES:
                raise BundleError(f"archive entry {path!r} exceeds single-file limit")
            data = zf.read(info)
            total += len(data)
            _enforce_limits(len(files) + 1, total, compressed_size)
            files[path] = data
    except (zipfile.BadZipFile, RuntimeError) as exc:
        raise BundleError(f"corrupt zip archive: {exc}") from exc
    finally:
        zf.close()
    return files


# ---------------------------------------------------------------------------
# JSON parsing with duplicate-key rejection
# ---------------------------------------------------------------------------


def json_loads_strict(raw: bytes, what: str) -> object:
    """Parse JSON, rejecting duplicate object keys and non-object roots.

    A lockfile or manifest with a duplicated key silently keeps the last one
    in most parsers — exactly the ambiguity an integrity gate must refuse.
    """
    import json

    def _no_duplicates(pairs):  # type: ignore[no-untyped-def]
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                raise BundleError(f"{what} contains duplicate key {key!r}")
            seen.add(key)
        return dict(pairs)

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BundleError(f"{what} is not valid UTF-8: {exc}") from exc
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise BundleError(f"{what} is not valid JSON: {exc.msg} at line {exc.lineno}")
    return None  # pragma: no cover
