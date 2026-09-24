"""Local ros2 bag access: validation, indexing, digests and corruption checks.

A bag is indexed exactly once when a session is created. The index assigns a
*stable* sequence number (1..N) to every message in the bag, independent of
any topic filter, so that a checkpoint position stays meaningful even if the
filter later changes.

Ordering is fully deterministic: messages are ordered by
``(published_timestamp_ns, topic_name, sha256(payload))``. The content-hash
component makes the order independent of the storage plugin's internal read
order for messages that share a timestamp (the requirement that equal
timestamps across topics be processed in a defined order).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rosbag2_py

METADATA_FILENAME = "metadata.yaml"


class BagNotFoundError(FileNotFoundError):
    """The bag URI does not exist on disk."""


class BagAccessError(PermissionError):
    """The bag URI lies outside the configured allowed roots."""


class CorruptBagError(RuntimeError):
    """The bag exists but cannot be opened/read or is internally inconsistent."""


@dataclass(frozen=True)
class IndexEntry:
    seq: int  # 1-based, stable across the whole bag regardless of filters
    topic: str
    topic_type: str
    timestamp_ns: int
    data_length: int
    data_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "topic": self.topic,
            "topic_type": self.topic_type,
            "timestamp_ns": self.timestamp_ns,
            "data_length": self.data_length,
            "data_sha256": self.data_sha256,
        }


@dataclass(frozen=True)
class FileDigest:
    path: str  # relative to the bag root
    size: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class TopicInfo:
    name: str
    type: str
    message_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "message_count": self.message_count}


@dataclass(frozen=True)
class BagIndex:
    uri: str
    storage_id: str
    starting_time_ns: int
    duration_ns: int
    topics: tuple[TopicInfo, ...]
    entries: tuple[IndexEntry, ...]
    files: tuple[FileDigest, ...]
    message_index_sha256: str
    total_payload_bytes: int

    # -- serialised summaries ------------------------------------------------

    def topic_names(self) -> list[str]:
        return [t.name for t in self.topics]

    def digest_summary(self) -> dict[str, Any]:
        """Everything a checkpoint pins so that source changes can be detected."""
        return {
            "uri": self.uri,
            "storage_id": self.storage_id,
            "starting_time_ns": self.starting_time_ns,
            "duration_ns": self.duration_ns,
            "message_count": len(self.entries),
            "total_payload_bytes": self.total_payload_bytes,
            "message_index_sha256": self.message_index_sha256,
            "files": [f.to_dict() for f in self.files],
            "topics": [t.to_dict() for t in self.topics],
        }

    def describe(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "storage_id": self.storage_id,
            "starting_time_ns": self.starting_time_ns,
            "duration_ns": self.duration_ns,
            "message_count": len(self.entries),
            "total_payload_bytes": self.total_payload_bytes,
            "message_index_sha256": self.message_index_sha256,
            "topics": [t.to_dict() for t in self.topics],
            "files": [f.to_dict() for f in self.files],
        }


# ---------------------------------------------------------------------------
# URI handling
# ---------------------------------------------------------------------------


def resolve_bag_uri(raw_uri: str, allowed_roots: list[Path]) -> Path:
    """Resolve a client-supplied bag URI and enforce the allow-list.

    Either the bag directory (containing ``metadata.yaml``) or a concrete
    storage file inside it may be given; the containing directory is used.
    """
    if not raw_uri or not str(raw_uri).strip():
        raise BagNotFoundError("empty bag uri")
    p = Path(raw_uri).expanduser()
    if not p.is_absolute():
        # Relative paths are resolved against the first allowed root (and CWD).
        candidates = [root / p for root in allowed_roots] + [Path.cwd() / p]
        for c in candidates:
            if c.exists():
                p = c
                break
        else:
            p = candidates[0]
    p = p.resolve()
    if not p.exists():
        raise BagNotFoundError(f"bag path does not exist: {p}")

    bag_dir = p if p.is_dir() else p.parent
    if not (bag_dir / METADATA_FILENAME).is_file():
        raise CorruptBagError(
            f"{bag_dir} is not a ros2 bag directory: {METADATA_FILENAME} missing"
        )

    inside = any(
        bag_dir == root or root in bag_dir.parents for root in allowed_roots
    )
    if not inside:
        raise BagAccessError(f"bag {bag_dir} is outside REPLAY_BAG_ROOTS")
    return bag_dir


def _detect_storage_id(bag_dir: Path) -> str:
    """Read the storage identifier straight from metadata.yaml (no YAML dep)."""
    meta = bag_dir / METADATA_FILENAME
    try:
        text = meta.read_text(encoding="utf-8")
    except OSError as exc:
        raise CorruptBagError(f"cannot read {meta}: {exc}") from exc
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("storage_identifier:"):
            value = stripped.split(":", 1)[1].strip().strip("'\"")
            if value:
                return value
    raise CorruptBagError(f"storage_identifier missing in {meta}")


def _hash_file(path: Path) -> tuple[int, str]:
    h = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                h.update(chunk)
    except OSError as exc:
        raise CorruptBagError(f"cannot read storage file {path}: {exc}") from exc
    return size, h.hexdigest()


# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------


def index_bag(bag_dir: Path) -> BagIndex:
    """Open the bag and build the deterministic, fully-materialised index."""
    index, _payloads = index_bag_with_payloads(bag_dir)
    return index


def verify_index_fresh(index: BagIndex) -> None:
    """Re-hash the on-disk files and raise on any source change.

    The message index hash is only recomputed when a caller wants a deep check
    (it requires a full scan); file hashing is cheap and catches ordinary
    edits/rewrites of the bag.
    """
    bag_dir = Path(index.uri)
    current_files: dict[str, FileDigest] = {}
    expected_paths = {f.path for f in index.files}

    on_disk = {p.name for p in bag_dir.glob("*") if p.is_file()}
    extra = on_disk - expected_paths
    # The service never writes into bag dirs; an extra data file means the
    # source has changed.
    extra_data = {n for n in extra if n != METADATA_FILENAME}
    if extra_data:
        raise CorruptBagError(f"bag directory contains new files: {sorted(extra_data)}")

    for f in index.files:
        path = bag_dir / f.path
        if not path.is_file():
            raise CorruptBagError(f"storage file disappeared: {f.path}")
        size, digest = _hash_file(path)
        if size != f.size or digest != f.sha256:
            raise CorruptBagError(f"bag file changed on disk: {f.path}")
        current_files[f.path] = FileDigest(f.path, size, digest)


def deep_verify(index: BagIndex) -> None:
    """Full re-index and compare (used before accepting a checkpoint restore)."""
    verify_index_fresh(index)
    fresh = index_bag(Path(index.uri))
    if fresh.message_index_sha256 != index.message_index_sha256:
        raise CorruptBagError("message content/order differs from checkpoint digest")
    if fresh.describe()["files"] != [f.to_dict() for f in index.files]:
        raise CorruptBagError("file manifest differs from checkpoint digest")


def read_payloads(bag_dir: Path, storage_id: str) -> dict[int, bytes]:
    """Re-read a bag into ``seq -> CDR bytes`` in the deterministic order.

    The ordering/sorting must be identical to :func:`index_bag`; callers that
    need both should use :func:`index_bag_with_payloads`.
    """
    return index_bag_with_payloads(bag_dir, storage_id_hint=storage_id)[1]


def index_bag_with_payloads(
    bag_dir: Path, *, storage_id_hint: str | None = None
) -> tuple[BagIndex, dict[int, bytes]]:
    """Index a bag and also return every payload keyed by stable seq."""
    storage_id = storage_id_hint or _detect_storage_id(bag_dir)
    uri = str(bag_dir)
    try:
        info = rosbag2_py.Info().read_metadata(uri, storage_id)
    except RuntimeError as exc:
        raise CorruptBagError(f"metadata unreadable for {uri}: {exc}") from exc

    reader = rosbag2_py.SequentialReader()
    try:
        try:
            reader.open(
                rosbag2_py.StorageOptions(uri=uri, storage_id=storage_id),
                rosbag2_py.ConverterOptions(
                    input_serialization_format="cdr", output_serialization_format="cdr"
                ),
            )
        except RuntimeError as exc:
            raise CorruptBagError(f"storage cannot be opened for {uri}: {exc}") from exc
        try:
            reader.set_read_order(
                rosbag2_py.ReadOrder(
                    sort_by=rosbag2_py.ReadOrderSortBy.PublishedTimestamp,
                    reverse=False,
                )
            )
        except RuntimeError:
            pass
        topic_types = {t.name: t.type for t in reader.get_all_topics_and_types()}
        raw: list[tuple[int, str, str, bytes]] = []
        try:
            while reader.has_next():
                topic, data, ts = reader.read_next()
                raw.append((int(ts), topic, topic_types.get(topic, ""), bytes(data)))
        except RuntimeError as exc:
            raise CorruptBagError(f"message stream unreadable in {uri}: {exc}") from exc
    finally:
        try:
            reader.close()
        except Exception:
            pass

    raw.sort(key=lambda item: (item[0], item[1], hashlib.sha256(item[3]).hexdigest()))
    payloads: dict[int, bytes] = {}
    entries: list[IndexEntry] = []
    index_hasher = hashlib.sha256()
    total_payload = 0
    for seq, (ts, topic, topic_type, data) in enumerate(raw, start=1):
        digest = hashlib.sha256(data).hexdigest()
        entries.append(
            IndexEntry(seq, topic, topic_type, ts, len(data), digest)
        )
        payloads[seq] = data
        total_payload += len(data)
        index_hasher.update(
            f"{seq}|{topic}|{topic_type}|{ts}|{len(data)}|{digest}\n".encode()
        )

    declared_topics = tuple(
        TopicInfo(
            name=t.topic_metadata.name,
            type=t.topic_metadata.type,
            message_count=int(t.message_count),
        )
        for t in sorted(info.topics_with_message_count, key=lambda x: x.topic_metadata.name)
    )
    declared_total = sum(t.message_count for t in declared_topics)
    if declared_total != len(entries):
        raise CorruptBagError(
            f"metadata declares {declared_total} messages but {len(entries)} were read"
        )

    file_digests: list[FileDigest] = []
    relative_paths = list(info.relative_file_paths)
    if not relative_paths:
        raise CorruptBagError("bag metadata lists no storage files")
    relative_paths.append(METADATA_FILENAME)
    seen: set[str] = set()
    for rel in sorted(relative_paths):
        if rel in seen:
            continue
        seen.add(rel)
        fpath = bag_dir / rel
        if not fpath.is_file():
            raise CorruptBagError(f"declared storage file missing: {rel}")
        size, digest = _hash_file(fpath)
        file_digests.append(FileDigest(path=rel, size=size, sha256=digest))

    starting_ns = int(info.starting_time.nanoseconds)
    if entries and starting_ns != entries[0].timestamp_ns:
        raise CorruptBagError(
            "metadata starting_time does not match the first indexed message"
        )

    index = BagIndex(
        uri=uri,
        storage_id=storage_id,
        starting_time_ns=starting_ns,
        duration_ns=int(info.duration.nanoseconds),
        topics=declared_topics,
        entries=tuple(entries),
        files=tuple(file_digests),
        message_index_sha256=index_hasher.hexdigest(),
        total_payload_bytes=total_payload,
    )
    return index, payloads
