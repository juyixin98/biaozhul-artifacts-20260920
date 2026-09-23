#!/usr/bin/env python3
"""Generate small, deterministic ROS2 bags for testing the replay service.

The generated bag deliberately contains:

* several topics sharing **identical timestamps** (``/tick`` and ``/tock`` are
  always written at the same instant) so the deterministic same-timestamp
  ordering can be verified;
* one higher-rate topic (``/events``) to exercise pacing;
* a header field carrying the logical sequence so a subscriber can prove it saw
  the exact expected messages in the exact expected order.

Run after sourcing ROS, e.g.::

    python3 scripts/generate_bag.py --uri bags/demo --seconds 5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import rosbag2_py
from example_interfaces.msg import Int64
from rclpy.serialization import serialize_message

BASE_NS = 1_000_000_000  # arbitrary fixed epoch so bags are reproducible
TICK_PERIOD_NS = 100_000_000  # /tick and /tock at 10 Hz
EVENT_PERIOD_NS = 50_000_000  # /events at 20 Hz


def _write_topic(writer: rosbag2_py.SequentialWriter, topic_id: int, name: str) -> None:
    writer.create_topic(
        rosbag2_py.TopicMetadata(
            topic_id, name, "example_interfaces/msg/Int64", "cdr"
        )
    )


def generate(uri: str, seconds: float, storage_id: str = "mcap") -> dict:
    out = Path(uri)
    out.parent.mkdir(parents=True, exist_ok=True)

    writer = rosbag2_py.SequentialWriter()
    writer.open(
        rosbag2_py.StorageOptions(uri=str(out), storage_id=storage_id),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    _write_topic(writer, 0, "/tick")
    _write_topic(writer, 1, "/tock")
    _write_topic(writer, 2, "/events")

    tick_n = int(seconds * 1e9 / TICK_PERIOD_NS)
    event_n = int(seconds * 1e9 / EVENT_PERIOD_NS)
    counts = {"/tick": 0, "/tock": 0, "/events": 0}

    # Same-timestamp pair: write /tock before /tick physically, so a correct
    # replay must still emit them in the deterministic (topic-name) order
    # /tick then /tock — proving ordering is canonical, not write order.
    for i in range(tick_n + 1):
        ts = BASE_NS + i * TICK_PERIOD_NS
        writer.write("/tock", serialize_message(Int64(data=i)), ts)
        writer.write("/tick", serialize_message(Int64(data=i)), ts)
        counts["/tock"] += 1
        counts["/tick"] += 1
    for i in range(event_n + 1):
        ts = BASE_NS + i * EVENT_PERIOD_NS
        writer.write("/events", serialize_message(Int64(data=i)), ts)
        counts["/events"] += 1

    writer.close()
    return {"uri": str(out), "counts": counts}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", required=True, help="output bag directory")
    parser.add_argument("--seconds", type=float, default=5.0)
    parser.add_argument("--storage", default="mcap")
    args = parser.parse_args()
    info = generate(args.uri, args.seconds, args.storage)
    print(f"wrote bag {info['uri']}")
    for topic, count in sorted(info["counts"].items()):
        print(f"  {topic}: {count} messages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
