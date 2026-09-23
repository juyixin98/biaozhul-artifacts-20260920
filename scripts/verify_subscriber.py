#!/usr/bin/env python3
"""Subscriber node that verifies replayed messages over real DDS.

It subscribes to the replayed topics (``<prefix>/<topic>``), records the
received sequence numbers (the Int64 payload) and optionally asserts:

* no gaps or duplicates within a generation;
* messages carrying the original bag timestamp arrive monotonically;
* same-timestamp topics arrive in the documented canonical order
  (``/tick`` before ``/tock``).

It can run standalone (print what it sees for a fixed duration) or, via
``--expect``, exit non-zero if the expected counts are not observed — used by
the acceptance flow.

Example::

    python3 scripts/verify_subscriber.py --duration 6 --expect tick=51,tock=51
"""
from __future__ import annotations

import argparse
import sys
import threading
import time

import rclpy
from example_interfaces.msg import Int64


class Verifier:
    def __init__(self, prefix: str, topics: list[str]) -> None:
        rclpy.init()
        self.node = rclpy.create_node("replay_verifier")
        self.prefix = prefix
        self._lock = threading.Lock()
        self.received: dict[str, list[int]] = {t: [] for t in topics}
        self.order: list[tuple[str, int]] = []
        self._subs = []
        for topic in topics:
            target = self._target(topic)
            self._subs.append(
                self.node.create_subscription(
                    Int64, target, self._make_cb(topic), 100
                )
            )
            self.node.get_logger().info(f"subscribed to {target}")

    def _target(self, topic: str) -> str:
        if not self.prefix:
            return topic
        return self.prefix.rstrip("/") + "/" + topic.lstrip("/")

    def _make_cb(self, topic: str):
        def cb(msg: Int64) -> None:
            with self._lock:
                self.received[topic].append(msg.data)
                self.order.append((topic, msg.data))
        return cb

    def spin(self, duration: float) -> None:
        end = time.monotonic() + duration
        while time.monotonic() < end and rclpy.ok():
            rclpy.spin_once(self.node, timeout_sec=0.1)

    def shutdown(self) -> None:
        self.node.destroy_node()
        rclpy.shutdown()

    def check_same_timestamp_order(self) -> tuple[bool, str]:
        """Within each sequence index, /tick must be seen before /tock."""
        with self._lock:
            order = list(self.order)
        last_tock: dict[int, int] = {}
        seen_tick: set[int] = set()
        for pos, (topic, value) in enumerate(order):
            if topic == "/tick":
                seen_tick.add(value)
            elif topic == "/tock":
                last_tock[value] = pos
                if value not in seen_tick:
                    # Possible if /tock observed first; verify ordering list.
                    return False, f"/tock {value} observed before its /tick"
        return True, ""

    def check_sequences(self) -> tuple[bool, str]:
        with self._lock:
            snapshot = {t: list(v) for t, v in self.received.items()}
        for topic, values in snapshot.items():
            if values != sorted(values):
                return False, f"{topic}: payloads not monotonic: {values[:10]}"
            if len(set(values)) != len(values):
                return False, f"{topic}: duplicate payloads detected"
        return True, ""


def parse_expect(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    out = {}
    for part in raw.split(","):
        k, v = part.split("=")
        out[k.strip()] = int(v)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="/replay")
    parser.add_argument(
        "--topics",
        default="/tick,/tock,/events",
        help="comma separated source topic names",
    )
    parser.add_argument("--duration", type=float, default=6.0)
    parser.add_argument(
        "--expect",
        default=None,
        help="expected exact counts, e.g. tick=51,tock=51,events=101",
    )
    args = parser.parse_args()

    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    verifier = Verifier(args.prefix, topics)
    try:
        verifier.spin(args.duration)
        ok, why = verifier.check_sequences()
        if ok:
            ok, why = verifier.check_same_timestamp_order()

        counts = {t.lstrip("/"): len(v) for t, v in verifier.received.items()}
        print("received counts:", counts)
        print("first arrivals:", {t: v[:5] for t, v in verifier.received.items()})

        expect = parse_expect(args.expect)
        for name, want in expect.items():
            got = counts.get(name, 0)
            if got != want:
                ok = False
                why = f"{name}: expected {want}, got {got}"

        if ok:
            print("VERIFY OK: ordering and counts satisfied")
            return 0
        print(f"VERIFY FAILED: {why}", file=sys.stderr)
        return 1
    finally:
        verifier.shutdown()


if __name__ == "__main__":
    sys.exit(main())
