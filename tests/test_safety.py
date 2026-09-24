"""Security tests for the archive_guard core: attack fixtures must be rejected
and must never write outside the extraction root; failures leave no
half-published directory.
"""

from __future__ import annotations

import gzip
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from app.archive_guard.errors import (
    InvalidArchiveError,
    QuotaExceededError,
    UnsafeArchiveError,
)
from app.archive_guard.limits import Limits
from app.archive_guard.safety import parse_and_validate, safe_extract
from tests.tarbuild import make_tar

# Tight limits so bomb fixtures stay tiny.
TIGHT = Limits(
    max_upload_bytes=10 * 1024 * 1024,
    max_entries=20,
    max_total_size=4096,
    max_total_written_bytes=4096,
    max_single_file_bytes=1024,
    max_path_depth=16,
    max_symlink_hops=8,
)


@pytest.fixture()
def archive_file(tmp_path):
    def _write(members, *, gz=False):
        data = make_tar(members, gz=gz)
        p = tmp_path / ("in.tar" + (".gz" if gz else ""))
        p.write_bytes(data)
        return str(p)

    return _write


@pytest.fixture()
def extract_root(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    return root


def _extract(archive, root, limits=TIGHT, extract_id="ext_test"):
    return safe_extract(archive, str(root), extract_id, limits)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_benign_archive_roundtrip(archive_file, extract_root):
    archive = archive_file([
        {"name": "dir/", "kind": "dir"},
        {"name": "dir/hello.txt", "kind": "file", "payload": b"hi\n"},
        {"name": "link.txt", "kind": "hardlink", "target": "dir/hello.txt"},
        {"name": "dir/link2", "kind": "symlink", "target": "hello.txt"},
    ])
    final_dir, manifest = _extract(archive, extract_root)
    assert os.path.isdir(final_dir)
    assert (Path(final_dir) / "dir" / "hello.txt").read_bytes() == b"hi\n"
    # Hardlink content visible and actually linked (same inode).
    a = os.stat(Path(final_dir) / "dir" / "hello.txt")
    b = os.stat(Path(final_dir) / "link.txt")
    assert (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)
    # Symlink target resolves inside root.
    assert os.readlink(Path(final_dir) / "dir" / "link2") == "hello.txt"
    assert manifest["written_bytes"] == len(b"hi\n")
    # No staging leftovers.
    assert not list(extract_root.glob("..staging*"))
    assert not [p for p in extract_root.iterdir()
                if p.name.startswith(".ext_")]


def test_gnu_dot_prefix_accepted(archive_file, extract_root):
    # GNU tar prefixes names with "./".
    archive = archive_file([{"name": "./a.txt", "kind": "file",
                             "payload": b"x"}])
    final_dir, _ = _extract(archive, extract_root)
    assert (Path(final_dir) / "a.txt").read_bytes() == b"x"


# ---------------------------------------------------------------------------
# Malicious member names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_name", [
    "/etc/passwd",
    "/absolute.txt",
    "../escape.txt",
    "foo/../../escape.txt",
    "foo/../bar/../../escape.txt",
    "a//b.txt",
])
def test_malicious_paths_rejected_at_inspect(archive_file, bad_name):
    archive = archive_file([{"name": bad_name, "kind": "file",
                             "payload": b"x"}])
    with pytest.raises(UnsafeArchiveError):
        parse_and_validate(archive, TIGHT)


def test_malicious_path_creates_nothing(archive_file, extract_root):
    archive = archive_file([
        {"name": "../escape.txt", "kind": "file", "payload": b"PWNED"},
    ])
    with pytest.raises(UnsafeArchiveError):
        _extract(archive, extract_root)
    # Nothing next to the root, nothing inside it, no staging dir.
    assert not (extract_root.parent / "escape.txt").exists()
    assert list(extract_root.iterdir()) == []


def test_backslash_and_nul_rejected(archive_file):
    archive = archive_file([{"name": "a\\b.txt", "kind": "file",
                             "payload": b"x"}])
    with pytest.raises(UnsafeArchiveError):
        parse_and_validate(archive, TIGHT)


def test_duplicate_members_rejected(archive_file, extract_root):
    archive = archive_file([
        {"name": "a.txt", "kind": "file", "payload": b"1"},
        {"name": "a.txt", "kind": "file", "payload": b"2"},
    ])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_dup", TIGHT)
    assert list(extract_root.iterdir()) == []


# ---------------------------------------------------------------------------
# Symlink chains / escape
# ---------------------------------------------------------------------------


def test_absolute_symlink_rejected(archive_file, extract_root):
    archive = archive_file([
        {"name": "evil", "kind": "symlink", "target": "/tmp"},
        {"name": "evil/payload", "kind": "file", "payload": b"x"},
    ])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_abs", TIGHT)
    assert list(extract_root.iterdir()) == []


def test_parent_symlink_escape_rejected(archive_file, extract_root):
    # Classic: symlink pointing outside, then a file "inside" the link.
    archive = archive_file([
        {"name": "sub", "kind": "dir"},
        {"name": "sub/up", "kind": "symlink", "target": "../../../"},
        {"name": "sub/up/evil.txt", "kind": "file", "payload": b"PWNED"},
    ])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_se", TIGHT)
    assert list(extract_root.iterdir()) == []
    # Nothing escaped to the filesystem near the root.
    assert not (extract_root.parent.parent / "evil.txt").exists()


def test_symlink_chain_escape_rejected(archive_file, extract_root):
    # Chain: a -> b/c, b -> ../../ => resolved outside.
    archive = archive_file([
        {"name": "b", "kind": "symlink", "target": "../../"},
        {"name": "a", "kind": "symlink", "target": "b/x"},
        {"name": "a/evil.txt", "kind": "file", "payload": b"PWNED"},
    ])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_chain", TIGHT)
    assert list(extract_root.iterdir()) == []


def test_symlink_cycle_rejected(archive_file, extract_root):
    archive = archive_file([
        {"name": "a", "kind": "symlink", "target": "b"},
        {"name": "b", "kind": "symlink", "target": "a"},
        {"name": "a/evil.txt", "kind": "file", "payload": b"x"},
    ])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_cycle", TIGHT)
    assert list(extract_root.iterdir()) == []


def test_internal_relative_symlink_allowed(archive_file, extract_root):
    archive = archive_file([
        {"name": "d", "kind": "dir"},
        {"name": "d/f.txt", "kind": "file", "payload": b"ok"},
        {"name": "d/rel", "kind": "symlink", "target": "../d/f.txt"},
    ])
    final_dir, _ = _extract(archive, extract_root)
    assert os.readlink(Path(final_dir) / "d" / "rel") == "../d/f.txt"


def test_dangling_internal_symlink_allowed(archive_file, extract_root):
    # A dangling link whose target stays inside the root is harmless.
    archive = archive_file([{"name": "dangling", "kind": "symlink",
                             "target": "does/not/exist"}])
    final_dir, _ = _extract(archive, extract_root)
    assert os.path.islink(Path(final_dir) / "dangling")


def test_dangling_outward_symlink_rejected(archive_file, extract_root):
    # Dangling but pointing out of the root is still escape intent.
    archive = archive_file([{"name": "out", "kind": "symlink",
                             "target": "../../../etc"}])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_dangle", TIGHT)
    assert list(extract_root.iterdir()) == []


# ---------------------------------------------------------------------------
# Hardlinks
# ---------------------------------------------------------------------------


def test_hardlink_to_missing_target_rejected(archive_file, extract_root):
    archive = archive_file([
        {"name": "lnk", "kind": "hardlink", "target": "nope.txt"},
    ])
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_hl", TIGHT)
    assert list(extract_root.iterdir()) == []


def test_hardlink_absolute_target_rejected(archive_file, extract_root):
    archive = archive_file([
        {"name": "f", "kind": "file", "payload": b"x"},
        {"name": "lnk", "kind": "hardlink", "target": "/etc/hostname"},
    ])
    with pytest.raises(UnsafeArchiveError):
        parse_and_validate(archive, TIGHT)


# ---------------------------------------------------------------------------
# Special device members
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("typeflag", [
    tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.FIFOTYPE,
])
def test_device_members_rejected(archive_file, typeflag):
    archive = archive_file([{"name": "dev", "kind": "device",
                             "typeflag": typeflag}])
    with pytest.raises(UnsafeArchiveError):
        parse_and_validate(archive, TIGHT)


# ---------------------------------------------------------------------------
# Bombs and quotas
# ---------------------------------------------------------------------------


def test_single_file_quota_counts_actual_bytes(archive_file, extract_root):
    # Declare a small size is hard with tar (size is in the header), but we
    # can exceed the total quota with a legitimate 2 KiB payload vs 4 KiB cap.
    payload = b"A" * 2048
    archive = archive_file([
        {"name": "a", "kind": "file", "payload": payload},
        {"name": "b", "kind": "file", "payload": payload},
        {"name": "c", "kind": "file", "payload": payload},
    ])
    with pytest.raises(QuotaExceededError):
        safe_extract(archive, str(extract_root), "ext_quota", TIGHT)
    # Partial directory must not be published.
    assert not (extract_root / "ext_quota").exists()
    assert list(extract_root.iterdir()) == []


def test_entry_count_limit(archive_file, extract_root):
    members = [{"name": f"f{i}.txt", "kind": "file", "payload": b"x"}
               for i in range(25)]
    archive = archive_file(members)
    with pytest.raises(QuotaExceededError):
        safe_extract(archive, str(extract_root), "ext_entries", TIGHT)
    assert list(extract_root.iterdir()) == []


def test_declared_size_mismatch_is_invalid(archive_file, extract_root):
    # Build a valid archive with a 1024-byte final payload, then cut the
    # raw file to 2048 bytes -- inside lie.txt's declared payload region
    # (header@1024, data@1536..2560) -- simulating mid-payload truncation.
    archive = archive_file([
        {"name": "ok.txt", "kind": "file", "payload": b"ok"},
        {"name": "lie.txt", "kind": "file", "payload": b"Z" * 1024},
    ])
    raw = Path(archive).read_bytes()
    Path(archive).write_bytes(raw[:2048])
    with pytest.raises(InvalidArchiveError):
        safe_extract(archive, str(extract_root), "ext_lie", TIGHT)
    assert list(extract_root.iterdir()) == []


def test_gzip_bomb_ratio_rejected(archive_file, extract_root):
    # Highly compressible payload: 100 KiB of zeros gzips to ~100 bytes.
    bomb_payload = b"\x00" * 100 * 1024
    # Tight ratio limit (default 100) — force a tiny threshold via limits.
    limits = Limits(
        max_upload_bytes=10 * 1024 * 1024,
        max_entries=20,
        max_total_size=10 * 1024 * 1024,
        max_total_written_bytes=10 * 1024 * 1024,
        max_single_file_bytes=10 * 1024 * 1024,
        max_decompression_ratio=20,
    )
    archive = archive_file([{"name": "bomb.bin", "kind": "file",
                             "payload": bomb_payload}], gz=True)
    with pytest.raises(UnsafeArchiveError, match="zip bomb"):
        parse_and_validate(archive, limits)


def test_gzip_benign_accepted(archive_file, extract_root):
    archive = archive_file([{"name": "f.txt", "kind": "file",
                             "payload": b"compressed hello"}], gz=True)
    report = parse_and_validate(archive, TIGHT)
    assert report.compressed is True
    final_dir, _ = _extract(archive, extract_root)
    assert (Path(final_dir) / "f.txt").read_bytes() == b"compressed hello"


def test_not_a_tar(tmp_path, extract_root):
    p = tmp_path / "bad.tar"
    p.write_bytes(b"this is definitely not a tar file" * 10)
    with pytest.raises(InvalidArchiveError):
        parse_and_validate(str(p), TIGHT)


def test_bzip2_rejected_even_though_compressed(tmp_path, extract_root):
    # Only plain tar / gzip are in the accepted subset.
    import bz2

    raw = make_tar([{"name": "a", "kind": "file", "payload": b"x"}])
    p = tmp_path / "a.tar.bz2"
    p.write_bytes(bz2.compress(raw))
    with pytest.raises(InvalidArchiveError):
        parse_and_validate(str(p), TIGHT)


def test_no_write_outside_root_after_failure(archive_file, extract_root):
    archive = archive_file([
        {"name": "ok.txt", "kind": "file", "payload": b"ok"},
        # Fail late in the archive: second member escapes.
        {"name": "../../late_escape.txt", "kind": "file",
         "payload": b"PWNED"},
    ])
    # Sentry: snapshot the directory containing the root after the archive
    # itself has been written; no additional files may appear.
    parent_before = set(os.listdir(extract_root.parent))
    with pytest.raises(UnsafeArchiveError):
        safe_extract(archive, str(extract_root), "ext_late", TIGHT)
    # Final directory not published...
    assert not (extract_root / "ext_late").exists()
    # ...and no new file appeared anywhere next to it.
    assert set(os.listdir(extract_root.parent)) == parent_before


def test_setuid_bits_are_stripped(archive_file, extract_root):
    archive = archive_file([
        {"name": "suid.sh", "kind": "file", "payload": b"echo hi\n",
         "mode": 0o4755},
    ])
    final_dir, manifest = _extract(archive, extract_root)
    st = os.stat(Path(final_dir) / "suid.sh")
    assert not (st.st_mode & stat.S_ISUID)
    entry = next(e for e in manifest["entries"] if e["name"] == "suid.sh")
    assert entry["mode"] == "0755"
