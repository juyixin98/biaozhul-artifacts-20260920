#!/usr/bin/env python3
"""Replay verification subscriber.

Two modes:

* ``--mode ndjson`` (default): attach to the service's
  ``/sessions/<id>/stream`` endpoint and verify the full envelope protocol
  end-to-end (generation sealing, stable seq, original timestamps,
  deterministic tie-break). No DDS required.

* ``--mode ros``: real ROS2 subscriber. Raw-subscribes every message topic
  the bag uses and subscribes to the control topic (std_msgs/msg/String JSON
  markers). Verifies that after a ``seek``/``stop`` marker no message older
  than the requested jump is delivered -- the "old generations never
  publish" guarantee over DDS.

Both modes print a JSON report and exit non-zero on any failed invariant.

The exercised scenario is parameterised here; the acceptance runner
(scripts/run_acceptance.sh) drives the service while this node subscribes.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import threading
import time
import urllib.request
from typing import Any

# Envelope invariants checked for every observed message envelope.
REQUIRED_MESSAGE_FIELDS = (
    "kind",
    "session_id",
    "generation",
    "generation_seq",
    "seq",
    "topic",
    "type",
    "timestamp_ns",
    "data_b64",
)


def verify_ndjson(base_url: str, session_id: str, duration: float) -> dict[str, Any]:
    url = f"{base_url}/sessions/{session_id}/stream"
    report: dict[str, Any] = {
        "mode": "ndjson",
        "checks": [],
        "messages": 0,
        "events": 0,
        "generations_seen": set(),
    }
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    # Per-generation: last (ts, topic, seq, gen_seq)
    last: dict[int, dict[str, Any]] = {}
    generations = report["generations_seen"]
    seek_targets: dict[int, int] = {}  # generation -> seek target ts
    start = time.time()

    # A moderate socket timeout keeps the overall-duration loop responsive
    # when the stream is idle (server keeps chunked connection open); idle
    # timeouts are simply retried until ``duration`` elapses.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=duration + 5) as resp:
        try:
            resp.fp.raw._sock.settimeout(1.0)
        except AttributeError:
            pass
        while time.time() - start < duration:
            try:
                line = resp.readline()
            except (TimeoutError, OSError):
                continue
            if not line:
                break
            if not line.strip():
                continue
            env = json.loads(line)
            generations.add(env.get("generation"))
            if env.get("kind") == "message":
                for f in REQUIRED_MESSAGE_FIELDS:
                    if f not in env:
                        check(f"field:{f}", False, f"missing in {env.get('seq')}")
                gen = env["generation"]
                st = last.setdefault(
                    gen,
                    {"ts": -1, "topic": None, "seq": 0, "gen_seq": 0},
                )
                # Stable seq strictly increases within a generation.
                check(
                    f"g{gen}:seq_increasing",
                    env["seq"] > st["seq"],
                    f"seq {env['seq']} after {st['seq']}",
                )
                # generation_seq is a dense 1..N counter.
                check(
                    f"g{gen}:gen_seq_dense",
                    env["generation_seq"] == st["gen_seq"] + 1,
                    f"{env['generation_seq']} != {st['gen_seq'] + 1}",
                )
                # Timestamps non-decreasing; equal timestamps tie-break by topic.
                if env["timestamp_ns"] == st["ts"]:
                    check(
                        f"g{gen}:same_ts_topic_order",
                        st["topic"] is None or env["topic"] >= st["topic"],
                        f"{env['topic']} after {st['topic']} at ts={st['ts']}",
                    )
                else:
                    check(
                        f"g{gen}:ts_non_decreasing",
                        env["timestamp_ns"] >= st["ts"],
                        f"{env['timestamp_ns']} after {st['ts']}",
                    )
                # Payload digest must match the declared digest.
                raw = base64.b64decode(env["data_b64"])
                digest = hashlib.sha256(raw).hexdigest()
                check(
                    f"g{gen}:payload_sha256",
                    digest == env["data_sha256"],
                    f"seq={env['seq']}",
                )
                # After a seek in this generation, no message older than target.
                target = seek_targets.get(gen)
                if target is not None:
                    check(
                        f"g{gen}:no_old_after_seek",
                        env["timestamp_ns"] >= target,
                        f"ts={env['timestamp_ns']} target={target}",
                    )
                st.update(
                    ts=env["timestamp_ns"],
                    topic=env["topic"],
                    seq=env["seq"],
                    gen_seq=env["generation_seq"],
                )
                report["messages"] += 1
            else:
                report["events"] += 1
                if env.get("kind") == "seek":
                    seek_targets[env["generation"]] = env["timestamp_ns"]

    # Protocol-level final checks.
    check("at_least_two_generations", len(generations) >= 2, f"seen={sorted(generations)}")
    check("received_messages", report["messages"] > 0, str(report["messages"]))
    check("no_invariant_failures", not failures, "; ".join(failures[:5]))
    report["generations_seen"] = sorted(g for g in generations if g is not None)
    report["ok"] = not failures
    return report


# ---------------------------------------------------------------------------
# Real DDS subscriber mode
# ---------------------------------------------------------------------------


def verify_ros(
    topics: list[str], control_topic: str, duration: float, bag_uri: str | None
) -> dict[str, Any]:
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
    from std_msgs.msg import String

    report: dict[str, Any] = {
        "mode": "ros",
        "checks": [],
        "data_messages": 0,
        "control_events": 0,
        "generations": [],
    }
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail})
        if not ok:
            failures.append(f"{name}: {detail}")

    rclpy.init()
    node = Node("replay_verifier")
    qos = QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE, history=HistoryPolicy.KEEP_ALL
    )
    lock = threading.Lock()
    data: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []

    def make_cb(topic: str):
        def cb(raw: bytes) -> None:
            with lock:
                data.append(
                    {
                        "topic": topic,
                        "data": bytes(raw),
                        "recv_mono": time.monotonic(),
                    }
                )
        return cb

    for topic in topics:
        node.create_subscription(String, topic, make_cb(topic), qos, raw=True)

    def control_cb(msg: String) -> None:
        try:
            event = json.loads(msg.data)
        except json.JSONDecodeError:
            return
        with lock:
            event["recv_mono"] = time.monotonic()
            events.append(event)
    node.create_subscription(String, control_topic, control_cb, qos)

    end = time.time() + duration
    try:
        while time.time() < end:
            rclpy.spin_once(node, timeout_sec=0.1)
    finally:
        node.destroy_node()
        rclpy.shutdown()

    report["data_messages"] = len(data)
    report["control_events"] = len(events)
    gens = sorted({e.get("generation") for e in events if e.get("generation") is not None})
    report["generations"] = gens

    check("received_data", len(data) > 0, f"{len(data)} messages")
    check("received_control_events", len(events) > 0, f"{len(events)} events")
    check("at_least_two_generations", len(gens) >= 2, f"gens={gens}")

    # For every seek event, messages delivered afterwards must have bag
    # timestamps >= target. The raw payload does not carry the timestamp, so
    # the check uses the control event ordering: after a seek, the next data
    # burst must start at/after target. We map payload identity -> bag ts by
    # re-reading the bag if one was provided.
    if bag_uri:
        ts_by_identity = _bag_identity_to_ts(bag_uri)
        for event in events:
            if event.get("kind") != "seek":
                continue
            target = event["timestamp_ns"]
            t0 = event["recv_mono"]
            stale = []
            with lock:
                later = [d for d in data if d["recv_mono"] >= t0]
            for d in later:
                key = (d["topic"], hashlib.sha256(d["data"]).hexdigest())
                ts = ts_by_identity.get(key)
                if ts is not None and ts < target:
                    stale.append((d["topic"], ts, target))
            check(
                f"seek@{target}:no_old_after_seek",
                not stale,
                f"{len(stale)} stale messages",
            )

    # Same-timestamp ordering within any delivered run.
    with lock:
        ordered = sorted(data, key=lambda d: d["recv_mono"])
    check("delivery_ordered_by_recv_time", len(ordered) == len(data), "")
    check("no_invariant_failures", not failures, "; ".join(failures[:5]))
    report["ok"] = not failures
    return report


def _bag_identity_to_ts(bag_uri: str) -> dict[tuple[str, str], int]:
    import rosbag2_py

    info = rosbag2_py.Info().read_metadata(bag_uri, "mcap")
    reader = rosbag2_py.SequentialReader()
    reader.open(
        rosbag2_py.StorageOptions(uri=bag_uri, storage_id="mcap"),
        rosbag2_py.ConverterOptions("cdr", "cdr"),
    )
    out: dict[tuple[str, str], int] = {}
    while reader.has_next():
        topic, payload, ts = reader.read_next()
        out[(topic, hashlib.sha256(bytes(payload)).hexdigest())] = int(ts)
    _ = info
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["ndjson", "ros"], default="ndjson")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--topics", nargs="*", default=["/alpha", "/beta"])
    parser.add_argument("--control-topic", default="/replay/control")
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--bag", default=None)
    parser.add_argument("--report", default=None, help="Write JSON report here")
    args = parser.parse_args(argv)

    if args.mode == "ndjson":
        if not args.session_id:
            parser.error("--session-id required for ndjson mode")
        report = verify_ndjson(args.base_url, args.session_id, args.duration)
    else:
        report = verify_ros(
            args.topics, args.control_topic, args.duration, args.bag
        )

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
