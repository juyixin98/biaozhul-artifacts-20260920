"""Bag loading, integrity hashing and the deterministic message index.

The replay engine never streams straight from a ``SequentialReader``: doing so
leaves multi-topic, same-timestamp ordering at the mercy of the storage plugin
(mcap warns that published-timestamp ordering is not implemented). Instead we
read every message once, keep a *lightweight* entry per message and sort it into
one canonical, reproducible order. Each entry receives a stable sequence
number (its position in that order). Pause / speed / seek all operate on that
sequence, so semantics are fully under our control.

Only metadata and raw CDR bytes are held. The bytes are needed anyway to
publish, so the index doubles as the message cache for the (small, local test)
bags this service targets.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import rosbag2_py

CDR = "cdr"
CHUNK = 1024 * 1024


class BagError(Exception):
    """Raised when a bag cannot be opened, validated or read."""


@dataclass(frozen=True)
class TopicInfo:
    name: str
    type: str
    serialization_format: str
    message_count: int


@dataclass(frozen=True)
class IndexEntry:
    """One message positioned in the canonical global order."""

    seq: int  # stable, zero-based global sequence number
    topic: str
    timestamp_ns: int
    order_key: int  # position within its source file, used as final tie-break
    data: bytes = field(repr=False)


@dataclass
class BagIndex:
    uri: str
    storage_id: str
    start_ns: int
    end_ns: int  # timestamp of the final message
    duration_ns: int
    topics: dict[str, TopicInfo]
    entries: list[IndexEntry]
    files: dict[str, str]  # relative path -> sha256

    def __len__(self) -> int:
        return len(self.entries)

    def filtered_entries(self, topics: frozenset[str] | None) -> list[IndexEntry]:
        if not topics:
            return self.entries
        return [e for e in self.entries if e.topic in topics]

    def summary(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "storage_id": self.storage_id,
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "duration_ns": self.duration_ns,
            "message_count": len(self.entries),
            "files": self.files,
            "topics": {
                name: {
                    "type": t.type,
                    "message_count": t.message_count,
                    "serialization_format": t.serialization_format,
                }
                for name, t in sorted(self.topics.items())
            },
        }


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_bag_files(bag_dir: Path) -> dict[str, str]:
    """Return ``relative_path -> sha256`` for every regular file in the bag.

    Hashing every file (data *and* metadata.yaml) means any edit to message
    contents, topic metadata or the file set changes the digest, which is what
    checkpoint restore validation compares against.
    """
    if not bag_dir.exists():
        raise BagError(f"bag directory does not exist: {bag_dir}")
    if not bag_dir.is_dir():
        raise BagError(f"bag path is not a directory: {bag_dir}")

    files: dict[str, str] = {}
    for path in sorted(bag_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(bag_dir).as_posix()
        files[rel] = _sha256_file(path)
    if not files:
        raise BagError(f"bag directory contains no files: {bag_dir}")
    return files


def _metadata_file(bag_dir: Path) -> Path:
    return bag_dir / "metadata.yaml"


def _detect_storage(bag_dir: Path) -> str:
    """Prefer mcap; fall back to whatever storage plugin can open the bag."""
    try:
        return str(rosbag2_py.get_default_storage_id()) or "mcap"
    except Exception:  # pragma: no cover - defensive
        return "mcap"


def load_bag(bag_dir: Path, storage_id: str | None = None) -> BagIndex:
    """Open a bag, verify it is readable and build the canonical index.

    Raises :class:`BagError` for missing metadata, unknown storage, corruption
    or any read failure. The full read happens up front so corruption surfacing
    late in a file is detected at load time rather than mid-replay.
    """
    bag_dir = bag_dir.resolve()
    if not _metadata_file(bag_dir).exists():
        raise BagError(f"missing metadata.yaml in {bag_dir}")

    storage_id = storage_id or _detect_storage(bag_dir)
    reader = rosbag2_py.SequentialReader()
    storage_opts = rosbag2_py.StorageOptions(
        uri=str(bag_dir), storage_id=storage_id
    )
    converter_opts = rosbag2_py.ConverterOptions(
        input_serialization_format=CDR, output_serialization_format=CDR
    )
    try:
        reader.open(storage_opts, converter_opts)
    except Exception as exc:
        raise BagError(
            f"cannot open bag {bag_dir} with storage {storage_id}: {exc}"
        ) from exc

    # Enumerate in native (deterministic, file/receive) order, recording a
    # monotonic per-read position as the ultimate tie-breaker.
    raw: list[tuple[str, bytes, int, int]] = []
    position = 0
    # Capture topic metadata up front: it must be read before the reader closes.
    topic_types: dict[str, str] = {}
    topic_formats: dict[str, str] = {}
    for t in reader.get_all_topics_and_types():
        topic_types[t.name] = t.type
        topic_formats[t.name] = t.serialization_format
    try:
        while reader.has_next():
            topic, data, ts = reader.read_next()
            raw.append((topic, bytes(data), int(ts), position))
            position += 1
    except Exception as exc:
        raise BagError(f"error while reading bag {bag_dir}: {exc}") from exc
    finally:
        try:
            reader.close()
        except Exception:
            pass

    if not raw:
        raise BagError(f"bag contains no messages: {bag_dir}")

    # Canonical global order: published time, then topic name, then native
    # position. Python's sort is stable, so the last key guarantees a unique,
    # reproducible order even when topic and timestamp both tie.
    raw.sort(key=lambda r: (r[2], r[0], r[3]))

    entries: list[IndexEntry] = []
    counts: dict[str, int] = {}
    for seq, (topic, data, ts, order_key) in enumerate(raw):
        entries.append(
            IndexEntry(
                seq=seq,
                topic=topic,
                timestamp_ns=ts,
                order_key=order_key,
                data=data,
            )
        )
        counts[topic] = counts.get(topic, 0) + 1

    topics: dict[str, TopicInfo] = {}
    for topic in sorted(counts):
        topics[topic] = TopicInfo(
            name=topic,
            type=topic_types.get(topic, ""),
            serialization_format=topic_formats.get(topic, CDR),
            message_count=counts[topic],
        )

    start_ns = entries[0].timestamp_ns
    end_ns = entries[-1].timestamp_ns
    files = hash_bag_files(bag_dir)

    return BagIndex(
        uri=bag_dir.as_posix(),
        storage_id=storage_id,
        start_ns=start_ns,
        end_ns=end_ns,
        duration_ns=end_ns - start_ns,
        topics=topics,
        entries=entries,
        files=files,
    )


def files_match(index_files: dict[str, str], bag_dir: Path) -> tuple[bool, str]:
    """Compare a stored file digest map against the bag on disk.

    Returns ``(ok, reason)``. ``reason`` is empty on success and otherwise
    describes the first mismatch (missing file, added file or changed digest).
    """
    try:
        current = hash_bag_files(bag_dir)
    except BagError as exc:
        return False, str(exc)

    stored = dict(index_files)
    for rel, digest in stored.items():
        if rel not in current:
            return False, f"bag file removed: {rel}"
        if current[rel] != digest:
            return False, f"bag file changed: {rel}"
    added = sorted(set(current) - set(stored))
    if added:
        return False, f"bag file added: {added[0]}"
    return True, ""
