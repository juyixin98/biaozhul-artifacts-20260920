"""Core safety logic: tar preflight validation and quarantined extraction.

Threat model
------------
The archive is treated as attacker controlled.  We defend against:

* absolute paths          (``/etc/passwd``)
* directory traversal     (``../../etc/passwd``)
* NUL / control characters, Windows-style names
* symlink escape          (absolute targets, ``..`` targets, link chains
                          whose *resolved* path leaves the root, links used
                          as intermediate directories)
* hardlink escape         (hardlinks pointing outside the archive set)
* special device / FIFO members
* decompression / entry-count bombs, oversized files
* duplicate names

Members are first validated against an *abstract* in-archive graph (no
filesystem calls), so link chains are reasoned about before anything is
written.  Extraction then replays the validation and writes into a
staging directory that is atomically published (renamed) only after the
whole archive has been extracted successfully; on any failure the staging
directory is removed and no partial result is published.
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import tarfile
from dataclasses import dataclass, field

from .errors import (
    InvalidArchiveError,
    QuotaExceededError,
    UnsafeArchiveError,
)
from .limits import Limits

#: Member kinds supported by the accepted tar subset.
KIND_FILE = "file"
KIND_DIR = "dir"
KIND_SYMLINK = "symlink"
KIND_HARDLINK = "hardlink"

_COPY_CHUNK = 64 * 1024

# Type codes that tarfile *consumes internally* (long name / pax headers)
# are never yielded by iteration; everything outside the set below is
# rejected explicitly.
_ALLOWED_TYPES = frozenset(
    {
        tarfile.REGTYPE,
        tarfile.AREGTYPE,
        tarfile.DIRTYPE,
        tarfile.SYMTYPE,
        tarfile.LNKTYPE,
    }
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemberRecord:
    """One validated archive member."""

    kind: str
    #: Original name as stored in the tar header.
    raw_name: str
    #: Normalized name as a tuple of path components relative to root.
    name: tuple[str, ...]
    #: Declared payload size for regular files (tar header field).
    size: int
    mode: int
    #: Raw link target as stored (symlinks / hardlinks).
    raw_target: str | None = None
    #: Symlink target normalized *relative to the link's directory*,
    #: expressed as a root-relative tuple.
    target: tuple[str, ...] | None = None


@dataclass
class PreflightReport:
    members: list[MemberRecord] = field(default_factory=list)
    total_declared_bytes: int = 0
    compressed: bool = False
    compressed_bytes: int | None = None
    decompression_ratio: float | None = None

    @property
    def entry_count(self) -> int:
        return len(self.members)

    def kind_counts(self) -> dict[str, int]:
        counts = {KIND_FILE: 0, KIND_DIR: 0, KIND_SYMLINK: 0, KIND_HARDLINK: 0}
        for m in self.members:
            counts[m.kind] += 1
        return counts

    def to_dict(self) -> dict:
        return {
            "entry_count": self.entry_count,
            "kind_counts": self.kind_counts(),
            "total_declared_bytes": self.total_declared_bytes,
            "compressed": self.compressed,
            "compressed_bytes": self.compressed_bytes,
            "decompression_ratio": self.decompression_ratio,
            "entries": [
                {
                    "name": m.raw_name.rstrip("/"),
                    "kind": m.kind,
                    "size": m.size,
                    "mode": format(stat.S_IMODE(m.mode), "04o"),
                    **(
                        {"target": m.raw_target}
                        if m.kind in (KIND_SYMLINK, KIND_HARDLINK)
                        else {}
                    ),
                }
                for m in self.members
            ],
        }


# ---------------------------------------------------------------------------
# Name / target normalization
# ---------------------------------------------------------------------------


def _is_valid_component(part: str) -> bool:
    if part in ("", ".", ".."):
        return False
    # Reject NUL (defense in depth — tarfile already strips at NUL) and
    # other control characters / DEL.
    if any(ord(c) < 32 or ord(c) == 127 for c in part):
        return False
    # Reject lone surrogates that could smuggle through encodings.
    if any(0xD800 <= ord(c) <= 0xDFFF for c in part):
        return False
    # Backslash is a legal POSIX byte but a path separator on Windows; the
    # accepted subset forbids it to keep cross-platform semantics clear.
    if "\\" in part:
        return False
    return True


def normalize_member_name(raw: str, limits: Limits) -> tuple[str, ...]:
    """Normalize a member name; raise UnsafeArchiveError on any trick."""
    if not raw:
        raise UnsafeArchiveError("empty member name", member=raw)
    if "\x00" in raw:
        raise UnsafeArchiveError("NUL byte in member name", member=raw)
    if raw.startswith("/"):
        raise UnsafeArchiveError("absolute member path", member=raw)

    stripped = raw.rstrip("/")
    if not stripped:
        raise UnsafeArchiveError("empty member name", member=raw)

    parts: list[str] = []
    for part in stripped.split("/"):
        if part in ("", "."):
            # Empty component means "//"; "." is a GNU-tar style "./"
            # prefix. Empty components are rejected, "." is skipped
            # (harmless self-reference).
            if part == "":
                raise UnsafeArchiveError("empty path component", member=raw)
            continue
        if part == "..":
            raise UnsafeArchiveError("parent-directory traversal in member name",
                                     member=raw)
        if not _is_valid_component(part):
            raise UnsafeArchiveError(f"invalid path component {part!r}",
                                     member=raw)
        parts.append(part)

    if not parts:
        raise UnsafeArchiveError("member name resolves to archive root",
                                 member=raw)
    if len(parts) > limits.max_path_depth:
        raise UnsafeArchiveError(
            f"path depth {len(parts)} exceeds limit {limits.max_path_depth}",
            member=raw,
        )
    return tuple(parts)


def normalize_symlink_target(
    raw: str,
    link_dir: tuple[str, ...],
    limits: Limits,
    member_name: str,
) -> tuple[str, ...]:
    """Normalize a symlink body relative to its containing directory.

    The raw target follows POSIX symlink semantics: it is interpreted
    relative to the link's own directory.  Absolute targets and targets
    whose lexical resolution steps outside the archive root are rejected.
    """
    if raw is None or raw == "":
        raise UnsafeArchiveError("symlink with empty target", member=member_name)
    if "\x00" in raw:
        raise UnsafeArchiveError("NUL byte in symlink target", member=member_name)
    if len(raw) > limits.max_symlink_target_len:
        raise UnsafeArchiveError("symlink target too long", member=member_name)
    if raw.startswith("/"):
        raise UnsafeArchiveError("absolute symlink target", member=member_name)

    stack = list(link_dir)
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise UnsafeArchiveError(
                    "symlink target escapes archive root", member=member_name
                )
            stack.pop()
            continue
        if any(ord(c) < 32 or ord(c) == 127 for c in part):
            raise UnsafeArchiveError("control character in symlink target",
                                     member=member_name)
        if len(stack) + 1 > limits.max_path_depth:
            raise UnsafeArchiveError("symlink target too deep",
                                     member=member_name)
        stack.append(part)
    return tuple(stack)


def normalize_hardlink_target(
    raw: str, limits: Limits, member_name: str
) -> tuple[str, ...]:
    """Hardlink targets name another archive member: same rules as names."""
    if not raw:
        raise UnsafeArchiveError("hardlink with empty target", member=member_name)
    if "\x00" in raw:
        raise UnsafeArchiveError("NUL byte in hardlink target", member=member_name)
    # Hardlink references are member names, never absolute / containing "..".
    return normalize_member_name(raw, limits)


# ---------------------------------------------------------------------------
# Abstract link graph
# ---------------------------------------------------------------------------


def _build_member_map(
    records: list[MemberRecord],
) -> tuple[dict[tuple[str, ...], MemberRecord], set[tuple[str, ...]]]:
    """Map of every path that exists in the abstract archive.

    Explicit members plus implicit parent directories (created on demand
    during extraction).  Duplicate explicit names were already rejected.
    """
    members: dict[tuple[str, ...], MemberRecord] = {}
    known: set[tuple[str, ...]] = set()
    for rec in records:
        members[rec.name] = rec
        known.add(rec.name)

    for rec in records:
        for i in range(1, len(rec.name)):
            ancestor = rec.name[:i]
            known.add(ancestor)  # implicit directory
    return members, known


def resolve_path(
    path: tuple[str, ...],
    symlinks: dict[tuple[str, ...], tuple[str, ...]],
    limits: Limits,
) -> tuple[str, ...]:
    """Resolve a root-relative path through the abstract symlink graph.

    Kernel-style walk: when a path component is itself a symlink, the
    (already root-normalized) target is substituted and resolution
    restarts with the remaining suffix.  Cycles and chains longer than
    ``max_symlink_hops`` are rejected.
    """
    base: list[str] = []
    tokens: list[str] = list(path)
    hops = 0
    while tokens:
        comp = tokens.pop(0)
        candidate = tuple(base + [comp])
        if candidate in symlinks:
            hops += 1
            if hops > limits.max_symlink_hops:
                raise UnsafeArchiveError(
                    f"symlink chain exceeds {limits.max_symlink_hops} hops "
                    "(possible link cycle)",
                    member="/".join(path),
                )
            # Target is root-relative (normalized against link dir); the
            # remaining suffix follows it.
            tokens = list(symlinks[candidate]) + tokens
            base = []
            continue
        base.append(comp)
    return tuple(base)


def _validate_graph(
    records: list[MemberRecord], limits: Limits
) -> dict[tuple[str, ...], MemberRecord]:
    members, _known = _build_member_map(records)
    symlinks = {
        rec.name: rec.target
        for rec in records
        if rec.kind == KIND_SYMLINK
    }

    for rec in records:
        if rec.kind == KIND_SYMLINK:
            # The dangling case is legal (a link may point at nothing),
            # but resolution must still terminate inside the root.
            resolve_path(rec.target, symlinks, limits)
            continue

        resolved = resolve_path(rec.name, symlinks, limits)
        if resolved != rec.name:
            raise UnsafeArchiveError(
                "member path traverses a symlink "
                f"(resolves to {'/'.join(resolved)})",
                member=rec.raw_name,
            )

        if rec.kind == KIND_HARDLINK:
            target_rec = members.get(rec.target)
            if target_rec is None:
                raise UnsafeArchiveError(
                    "hardlink target is not present in archive",
                    member=rec.raw_name,
                )
            if target_rec.kind != KIND_FILE:
                raise UnsafeArchiveError(
                    "hardlink target is not a regular file",
                    member=rec.raw_name,
                )
    return members


# ---------------------------------------------------------------------------
# Tar parsing
# ---------------------------------------------------------------------------


def _open_tar(path: str) -> tuple[tarfile.TarFile, bool]:
    """Open only the accepted subset: plain tar or gzip-compressed tar."""
    try:
        with open(path, "rb") as fh:
            magic = fh.read(2)
    except OSError as exc:
        raise InvalidArchiveError(f"cannot read upload: {exc}") from exc

    compressed = magic == b"\x1f\x8b"
    mode = "r:gz" if compressed else "r:"
    try:
        tf = tarfile.open(path, mode)
    except (tarfile.ReadError, tarfile.CompressionError, gzip.BadGzipFile,
            EOFError, OSError) as exc:
        raise InvalidArchiveError(f"not a readable tar archive: {exc}") from exc
    return tf, compressed


def _classify(ti: tarfile.TarInfo) -> str:
    if ti.type not in _ALLOWED_TYPES:
        name = {
            tarfile.CHRTYPE: "character device",
            tarfile.BLKTYPE: "block device",
            tarfile.FIFOTYPE: "FIFO",
            tarfile.CONTTYPE: "contiguous file",
            tarfile.GNUTYPE_SPARSE: "sparse file",
        }.get(ti.type, f"unsupported member type {ti.type!r}")
        raise UnsafeArchiveError(f"unsupported member type: {name}",
                                 member=ti.name)
    return {
        tarfile.REGTYPE: KIND_FILE,
        tarfile.AREGTYPE: KIND_FILE,
        tarfile.DIRTYPE: KIND_DIR,
        tarfile.SYMTYPE: KIND_SYMLINK,
        tarfile.LNKTYPE: KIND_HARDLINK,
    }[ti.type]


def parse_and_validate(path: str, limits: Limits) -> PreflightReport:
    """Parse + run every non-filesystem check.  Shared by inspect & extract."""
    tf, compressed = _open_tar(path)
    report = PreflightReport(compressed=compressed)
    records: list[MemberRecord] = []
    seen: set[tuple[str, ...]] = set()

    try:
        while True:
            try:
                ti = tf.next()
            except (tarfile.ReadError, tarfile.CompressionError,
                    gzip.BadGzipFile, EOFError, OSError) as exc:
                raise InvalidArchiveError(
                    f"corrupt archive while reading headers: {exc}"
                ) from exc
            if ti is None:
                break

            kind = _classify(ti)
            norm = normalize_member_name(ti.name, limits)
            if norm in seen:
                raise UnsafeArchiveError("duplicate member name", member=ti.name)
            seen.add(norm)

            if len(records) >= limits.max_entries:
                raise QuotaExceededError(
                    f"entry count exceeds limit of {limits.max_entries}",
                    member=ti.name,
                    limit=limits.max_entries,
                    actual=len(records) + 1,
                )

            target_norm: tuple[str, ...] | None = None
            raw_target: str | None = None
            size = ti.size if kind == KIND_FILE else 0

            if kind == KIND_SYMLINK:
                raw_target = ti.linkname
                target_norm = normalize_symlink_target(
                    ti.linkname, norm[:-1], limits, ti.name
                )
            elif kind == KIND_HARDLINK:
                raw_target = ti.linkname
                target_norm = normalize_hardlink_target(
                    ti.linkname, limits, ti.name
                )

            if kind == KIND_FILE:
                if ti.size < 0:
                    raise InvalidArchiveError(
                        "negative declared file size", member=ti.name
                    )
                if ti.size > limits.max_single_file_bytes:
                    raise QuotaExceededError(
                        f"file exceeds single-file limit of "
                        f"{limits.max_single_file_bytes} bytes",
                        member=ti.name,
                        limit=limits.max_single_file_bytes,
                        actual=ti.size,
                    )
                report.total_declared_bytes += ti.size
                if report.total_declared_bytes > limits.max_total_size:
                    raise QuotaExceededError(
                        f"declared total size exceeds limit of "
                        f"{limits.max_total_size} bytes",
                        member=ti.name,
                        limit=limits.max_total_size,
                        actual=report.total_declared_bytes,
                    )

            records.append(
                MemberRecord(
                    kind=kind,
                    raw_name=ti.name,
                    name=norm,
                    size=size,
                    mode=ti.mode,
                    raw_target=raw_target,
                    target=target_norm,
                )
            )
    finally:
        tf.close()

    # Whole-archive graph validation (link chains, hardlink targets).
    _validate_graph(records, limits)

    report.members = records

    if compressed:
        try:
            compressed_bytes = os.path.getsize(path)
        except OSError:
            compressed_bytes = None
        report.compressed_bytes = compressed_bytes
        if compressed_bytes and compressed_bytes > limits.max_compressed_bytes:
            raise QuotaExceededError(
                f"compressed archive exceeds "
                f"{limits.max_compressed_bytes} bytes",
                limit=limits.max_compressed_bytes,
                actual=compressed_bytes,
            )
        if compressed_bytes and report.total_declared_bytes > 0:
            ratio = report.total_declared_bytes / compressed_bytes
            report.decompression_ratio = ratio
            if ratio > limits.max_decompression_ratio:
                raise UnsafeArchiveError(
                    f"decompression ratio {ratio:.1f}x exceeds "
                    f"{limits.max_decompression_ratio}x (zip bomb)"
                )
    return report


# Backwards-compatible public name.
inspect_tar = parse_and_validate


# ---------------------------------------------------------------------------
# Filesystem extraction (staging -> atomic publish)
# ---------------------------------------------------------------------------


def _sanitize_mode(mode: int, default: int) -> int:
    """Drop setuid/setgid/sticky bits; never store special permissions."""
    m = stat.S_IMODE(mode) if mode else default
    m &= ~0o7000  # clear S_ISUID | S_ISGID | S_ISVTX
    return m


def _ensure_parents(
    staging: str, prefix: tuple[str, ...]
) -> None:
    """Materialize implicit ancestor directories inside staging.

    Every existing ancestor must be a real directory (never a symlink or
    file) — defense in depth on top of the abstract graph check.
    """
    cur = staging
    for part in prefix:
        cur = os.path.join(cur, part)
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            os.mkdir(cur, 0o700)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafeArchiveError(
                f"path prefix {'/'.join(prefix)} is not a directory"
            )


def safe_extract(
    path: str,
    dest_parent: str,
    extract_id: str,
    limits: Limits,
) -> tuple[str, dict]:
    """Validate then extract *path* under dest_parent/<extract_id>.

    Returns ``(final_dir, manifest)``.  Extraction happens in a hidden
    staging directory; only a fully successful run is renamed into place.
    On failure the staging directory is removed and ``final`` is never
    created.
    """
    if not extract_id or "/" in extract_id or extract_id in (".", "..") \
            or not all(c.isalnum() or c in "_-" for c in extract_id):
        raise ValueError("invalid extract id")

    os.makedirs(dest_parent, mode=0o700, exist_ok=True)
    final_dir = os.path.join(dest_parent, extract_id)
    if os.path.lexists(final_dir):
        raise InvalidArchiveError(f"extraction id already exists: {extract_id}")

    staging = os.path.join(
        dest_parent, f".{extract_id}.staging.{os.getpid()}"
    )
    if os.path.lexists(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.mkdir(staging, 0o700)

    manifest_entries: list[dict] = []
    total_written = 0
    try:
        report = parse_and_validate(path, limits)
        records_by_name = {m.name: m for m in report.members}

        tf, _ = _open_tar(path)
        deferred_hardlinks: list[tuple[MemberRecord, tarfile.TarInfo]] = []
        try:
            ordered: list[tuple[MemberRecord, tarfile.TarInfo]] = []
            for rec in report.members:
                ti = tf.next()
                if ti is None:
                    raise InvalidArchiveError(
                        "archive changed between validation and extraction"
                    )
                if normalize_member_name(ti.name, limits) != rec.name:
                    raise InvalidArchiveError(
                        "archive changed between validation and extraction",
                        member=ti.name,
                    )
                ordered.append((rec, ti))

            for rec, ti in ordered:
                target_fs = os.path.join(staging, *rec.name)
                _ensure_parents(staging, rec.name[:-1])

                if rec.kind == KIND_DIR:
                    try:
                        os.mkdir(target_fs, _sanitize_mode(ti.mode, 0o755))
                    except FileExistsError:
                        # Created implicitly earlier; apply explicit mode.
                        os.chmod(target_fs, _sanitize_mode(ti.mode, 0o755))
                    manifest_entries.append(
                        {"name": "/".join(rec.name), "kind": KIND_DIR,
                         "size": 0,
                         "mode": format(_sanitize_mode(ti.mode, 0o755), "04o")}
                    )

                elif rec.kind == KIND_FILE:
                    mode = _sanitize_mode(ti.mode, 0o644)
                    # O_EXCL: no clobbering duplicates. O_NOFOLLOW: never
                    # write through a pre-existing symlink.
                    fd = os.open(
                        target_fs,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        mode,
                    )
                    written = 0
                    src = tf.extractfile(ti)
                    if src is None:
                        os.close(fd)
                        raise InvalidArchiveError(
                            "cannot read member payload", member=ti.name
                        )
                    try:
                        with os.fdopen(fd, "wb") as out:
                            while True:
                                try:
                                    chunk = src.read(_COPY_CHUNK)
                                except (tarfile.ReadError, OSError) as exc:
                                    raise InvalidArchiveError(
                                        f"corrupt member payload: {exc}",
                                        member=ti.name,
                                    ) from exc
                                if not chunk:
                                    break
                                written += len(chunk)
                                total_written += len(chunk)
                                if written > limits.max_single_file_bytes:
                                    raise QuotaExceededError(
                                        "single-file byte quota exceeded "
                                        "while writing",
                                        member=ti.name,
                                        limit=limits.max_single_file_bytes,
                                        actual=written,
                                    )
                                if total_written > \
                                        limits.max_total_written_bytes:
                                    raise QuotaExceededError(
                                        "total byte quota exceeded while "
                                        "writing (actual bytes counted)",
                                        member=ti.name,
                                        limit=limits.max_total_written_bytes,
                                        actual=total_written,
                                    )
                                out.write(chunk)
                    finally:
                        if not src.closed:
                            src.close()

                    if written != ti.size:
                        raise InvalidArchiveError(
                            f"truncated payload: declared {ti.size}, "
                            f"read {written} bytes",
                            member=ti.name,
                        )
                    manifest_entries.append(
                        {"name": "/".join(rec.name), "kind": KIND_FILE,
                         "size": written, "mode": format(mode, "04o")}
                    )

                elif rec.kind == KIND_SYMLINK:
                    # os.symlink fails if the path exists (no replacement).
                    os.symlink(rec.raw_target, target_fs)
                    manifest_entries.append(
                        {"name": "/".join(rec.name), "kind": KIND_SYMLINK,
                         "size": 0, "target": rec.raw_target}
                    )

                elif rec.kind == KIND_HARDLINK:
                    deferred_hardlinks.append((rec, ti))

            # Hardlinks last: targets are guaranteed regular members and
            # have all been written by now.
            for rec, _ti in deferred_hardlinks:
                link_path = os.path.join(staging, *rec.name)
                target_path = os.path.join(staging, *rec.target)
                st = os.lstat(target_path)
                if not stat.S_ISREG(st.st_mode):
                    raise UnsafeArchiveError(
                        "hardlink target is not a regular file on disk",
                        member=rec.raw_name,
                    )
                os.link(target_path, link_path, follow_symlinks=False)
                manifest_entries.append(
                    {"name": "/".join(rec.name), "kind": KIND_HARDLINK,
                     "size": 0, "target": "/".join(rec.target)}
                )
        finally:
            tf.close()

        # Publish atomically.  final_dir was confirmed absent before
        # staging; re-check to avoid os.rename() silently replacing an
        # empty directory that appeared meanwhile.
        if os.path.lexists(final_dir):
            raise InvalidArchiveError(
                f"extraction id already exists: {extract_id}"
            )
        os.rename(staging, final_dir)

    except BaseException:
        # Never publish a half-extracted directory.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    manifest = {
        "extract_id": extract_id,
        "path": os.path.abspath(final_dir),
        "entries": manifest_entries,
        "entry_count": len(manifest_entries),
        "written_bytes": total_written,
    }
    return os.path.abspath(final_dir), manifest
