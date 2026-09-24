"""Generate small deterministic ROS2 bags (mcap) for tests and demos.

The generated messages are ``std_msgs/msg/String`` whose ``data`` field is
``"<topic>#<index>"`` (plus an optional JSON payload), so a subscriber can
verify both delivery and order without any custom message packages.

Corruption modes (for the corrupt-bag acceptance test):

* ``truncate_storage``  -- cut the tail off the .mcap file
* ``garbage_storage``   -- overwrite the start of the .mcap with random bytes
* ``missing_metadata``  -- delete metadata.yaml
* ``bad_metadata``      -- strip the storage_identifier line
* ``missing_storage``   -- delete every .mcap file
* ``extra_file``        -- add an untracked data file (freshness check)
* ``payload_tamper``    -- rewrite one message byte (rewrites a valid bag)
"""
from __future__ import annotations

import os
import random
import shutil
from pathlib import Path
from typing import Optional

import rosbag2_py
from rclpy.serialization import serialize_message
from std_msgs.msg import String

MSG_TYPE = "std_msgs/msg/String"
CORRUPT_MODES = (
    "truncate_storage",
    "garbage_storage",
    "missing_metadata",
    "bad_metadata",
    "missing_storage",
    "extra_file",
    "payload_tamper",
)


def make_bag(
    output_uri: str | Path,
    *,
    topics: tuple[str, ...] = ("/alpha", "/beta"),
    messages: int = 20,
    gap_ns: int = 100_000_000,  # 100 ms between messages
    start_ns: int = 1_000_000_000,
    same_time_groups: int = 0,
    storage_id: str = "mcap",
    overwrite: bool = True,
) -> dict:
    """Create a bag.

    ``same_time_groups`` creates that many positions at which every topic gets
    a message with the *identical* timestamp, exercising the deterministic
    tie-break rule (ordered by topic name).

    Returns a small descriptor with timestamps and contents.
    """
    uri = Path(output_uri)
    if uri.exists():
        if not overwrite:
            raise FileExistsError(uri)
        shutil.rmtree(uri)
    uri.parent.mkdir(parents=True, exist_ok=True)

    writer = rosbag2_py.SequentialWriter()
    writer.open(
        rosbag2_py.StorageOptions(uri=str(uri), storage_id=storage_id),
        rosbag2_py.ConverterOptions(
            input_serialization_format="cdr", output_serialization_format="cdr"
        ),
    )
    for i, topic in enumerate(sorted(topics), start=1):
        writer.create_topic(
            rosbag2_py.TopicMetadata(
                id=i, name=topic, type=MSG_TYPE, serialization_format="cdr"
            )
        )

    records: list[tuple[str, int, str]] = []
    ts = start_ns
    written = 0
    i = 0
    while written < messages:
        if same_time_groups and i % 5 == 4 and written + len(topics) <= messages:
            # All topics at one timestamp.
            for topic in sorted(topics):
                data = f"{topic}#{written}"
                writer.write(
                    topic, bytes(serialize_message(String(data=data))), ts
                )
                records.append((topic, ts, data))
                written += 1
            ts += gap_ns
        else:
            topic = sorted(topics)[i % len(topics)]
            data = f"{topic}#{written}"
            writer.write(
                topic, bytes(serialize_message(String(data=data))), ts
            )
            records.append((topic, ts, data))
            written += 1
            ts += gap_ns
        i += 1

    writer.close()
    return {
        "uri": str(uri),
        "topics": list(sorted(topics)),
        "messages": records,
        "storage_id": storage_id,
    }


def make_corrupt(
    output_uri: str | Path,
    mode: str,
    *,
    seed: Optional[int] = None,
    **kwargs,
) -> Path:
    """Create a valid bag first, then damage it in the requested way."""
    if mode not in CORRUPT_MODES:
        raise ValueError(f"unknown corrupt mode {mode!r}; valid: {CORRUPT_MODES}")
    desc = make_bag(output_uri, **kwargs)
    bag_dir = Path(desc["uri"])

    if mode == "missing_metadata":
        (bag_dir / "metadata.yaml").unlink()
    elif mode == "bad_metadata":
        meta = bag_dir / "metadata.yaml"
        text = meta.read_text()
        text = "\n".join(
            line for line in text.splitlines() if not line.strip().startswith("storage_identifier:")
        )
        meta.write_text(text)
    elif mode == "missing_storage":
        for f in bag_dir.glob("*.mcap"):
            f.unlink()
    elif mode == "extra_file":
        (bag_dir / "rogue_data_0.mcap").write_bytes(os.urandom(128))
    else:
        storage_files = list(bag_dir.glob("*.mcap"))
        if not storage_files:
            raise RuntimeError("no storage file to corrupt")
        target = storage_files[0]
        if mode == "truncate_storage":
            size = target.stat().st_size
            # Cut into the message record area (mcap keeps data records near
            # the front; a half-size truncation may only remove the trailing
            # summary and still parse, so keep just a small header).
            keep = min(256, max(1, size // 4))
            with target.open("r+b") as fh:
                fh.truncate(keep)
        elif mode == "garbage_storage":
            rng = random.Random(seed)
            with target.open("r+b") as fh:
                fh.seek(0)
                fh.write(bytes(rng.randrange(256) for _ in range(256)))
        elif mode == "payload_tamper":
            # Flip a byte inside the message payload region of the mcap.
            size = target.stat().st_size
            rng = random.Random(seed or 0)
            with target.open("r+b") as fh:
                # Skip the first 64 bytes of headers, flip a byte in the body.
                pos = rng.randrange(64, max(65, size - 8))
                fh.seek(pos)
                b = fh.read(1)
                fh.seek(pos)
                fh.write(bytes([b[0] ^ 0xFF]))

    return bag_dir


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Generate a small ROS2 test bag")
    parser.add_argument("output", help="Output bag directory")
    parser.add_argument("--topics", nargs="+", default=["/alpha", "/beta"])
    parser.add_argument("--messages", type=int, default=20)
    parser.add_argument("--gap-ms", type=float, default=100.0)
    parser.add_argument("--same-time-groups", type=int, default=2)
    parser.add_argument(
        "--corrupt",
        choices=CORRUPT_MODES,
        default=None,
        help="Produce a corrupted bag for failure tests",
    )
    args = parser.parse_args(argv)

    common = dict(
        topics=tuple(args.topics),
        messages=args.messages,
        gap_ns=int(args.gap_ms * 1_000_000),
        same_time_groups=args.same_time_groups,
    )
    if args.corrupt:
        path = make_corrupt(args.output, args.corrupt, **common)
        print(f"CORRUPT bag ({args.corrupt}) written to {path}")
    else:
        desc = make_bag(args.output, **common)
        print(
            f"bag written to {desc['uri']}: {len(desc['messages'])} messages, "
            f"topics={desc['topics']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
