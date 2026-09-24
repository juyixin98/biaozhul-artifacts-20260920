"""Replay engine behaviour on the in-process loopback sink."""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.conftest import requires_ros


def _engine_for(bag_uri: Path, *, topics=None, rate=1.0, tick=0.002):
    from app import bagstore
    from app.playback import ReplayEngine
    from app.sinks import LoopbackSink

    index, payloads = bagstore.index_bag_with_payloads(bag_uri)
    sink = LoopbackSink()
    engine = ReplayEngine(
        index,
        sink,
        tick_seconds=tick,
        topic_filter=topics,
        payloads=payloads,
        session_id="test",
    )
    engine.set_rate(rate)
    return engine, sink, index


def _drain_messages(sink, timeout=3.0):
    """Return everything delivered on the sink (polls its history)."""
    end = time.time() + timeout
    last = 0
    while time.time() < end:
        items = sink.history(after_history_id=last)
        if items:
            last = items[-1]["history_id"]
        time.sleep(0.01)
    return [i for i in sink.history() if i.get("kind") == "message"]


def _wait_until(predicate, timeout=3.0, interval=0.005):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(interval)
    return False


@requires_ros
def test_play_all_preserves_order_and_original_timestamps(good_bag):
    engine, sink, index = _engine_for(Path(good_bag["uri"]), rate=50.0)
    try:
        engine.play()
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        engine.pause()
        msgs = [i for i in sink.history() if i.get("kind") == "message"]
        assert len(msgs) == len(index.entries)

        seqs = [m["seq"] for m in msgs]
        assert seqs == list(range(1, len(msgs) + 1))
        tss = [m["timestamp_ns"] for m in msgs]
        assert tss == sorted(tss)

        # timestamps in envelopes are exactly the bag timestamps, regardless
        # of the 50x rate
        for env, entry in zip(msgs, index.entries):
            assert env["timestamp_ns"] == entry.timestamp_ns
            assert env["data_sha256"] == entry.data_sha256
    finally:
        engine.shutdown()


@requires_ros
def test_pause_freezes_and_resume_continues(good_bag):
    engine, sink, _ = _engine_for(Path(good_bag["uri"]), rate=1.0)
    try:
        engine.play()
        time.sleep(0.12)  # at 50ms gap -> a few messages out
        engine.pause()
        frozen = engine.position()["next_seq"]
        time.sleep(0.2)
        assert engine.position()["next_seq"] == frozen  # nothing moves
        engine.play()
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
    finally:
        engine.shutdown()


@requires_ros
def test_rate_change_does_not_rewrite_message_timestamps(good_bag):
    engine, sink, index = _engine_for(Path(good_bag["uri"]), rate=1.0)
    try:
        engine.play()
        time.sleep(0.05)
        engine.set_rate(50.0)  # fast forward the rest
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        msgs = [i for i in sink.history() if i.get("kind") == "message"]
        # The set of delivered timestamps must equal the bag's own timeline.
        assert sorted(m["timestamp_ns"] for m in msgs) == [
            e.timestamp_ns for e in index.entries
        ]
    finally:
        engine.shutdown()


@requires_ros
def test_same_timestamp_deterministic_topic_order(good_bag):
    engine, sink, index = _engine_for(Path(good_bag["uri"]), rate=50.0)
    try:
        engine.play()
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        msgs = [i for i in sink.history() if i.get("kind") == "message"]
        by_ts: dict[int, list[str]] = {}
        for m in msgs:
            by_ts.setdefault(m["timestamp_ns"], []).append(m["topic"])
        shared = [t for t in by_ts.values() if len(t) > 1]
        assert shared  # fixture generates same-timestamp groups
        for topics in shared:
            assert topics == sorted(topics)
    finally:
        engine.shutdown()


@requires_ros
def test_topic_filter_only_emits_selected_topics(good_bag):
    engine, sink, index = _engine_for(
        Path(good_bag["uri"]), topics=["/alpha"], rate=50.0
    )
    try:
        engine.play()
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        msgs = [i for i in sink.history() if i.get("kind") == "message"]
        assert msgs, "alpha should have messages"
        assert all(m["topic"] == "/alpha" for m in msgs)
        # stable seq numbers still come from the whole bag (non-contiguous)
        alpha_entries = [e for e in index.entries if e.topic == "/alpha"]
        assert [m["seq"] for m in msgs] == [e.seq for e in alpha_entries]
    finally:
        engine.shutdown()


@requires_ros
def test_seek_mid_bag_opens_new_generation_and_replays_from_target(good_bag):
    engine, sink, index = _engine_for(Path(good_bag["uri"]), rate=50.0)
    try:
        engine.play()
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        assert engine.position()["finished"]

        target = 10
        snap = engine.seek(seq=target, play_after=True)
        new_gen = snap["generation"]
        assert new_gen >= 1
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)

        # All messages delivered under the new generation start at target.
        new_msgs = [
            i
            for i in sink.history()
            if i.get("kind") == "message" and i["generation"] == new_gen
        ]
        assert new_msgs
        assert [m["seq"] for m in new_msgs] == list(
            range(target, len(index.entries) + 1)
        )
        # gen_seq is dense within the new generation
        assert [m["generation_seq"] for m in new_msgs] == list(
            range(1, len(new_msgs) + 1)
        )
    finally:
        engine.shutdown()


@requires_ros
def test_seek_by_timestamp_lower_bound(good_bag):
    engine, sink, index = _engine_for(Path(good_bag["uri"]), rate=50.0)
    try:
        entry = index.entries[7]
        snap = engine.seek(timestamp_ns=entry.timestamp_ns, play_after=True)
        assert snap["next_seq"] == entry.seq
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        new_gen = snap["generation"]
        new_msgs = [
            i
            for i in sink.history()
            if i.get("kind") == "message" and i["generation"] == new_gen
        ]
        assert new_msgs[0]["seq"] == entry.seq
        assert all(m["timestamp_ns"] >= entry.timestamp_ns for m in new_msgs)
    finally:
        engine.shutdown()


@requires_ros
def test_seek_to_end_then_back_starts_fresh_generation(good_bag):
    """End-of-bag jump then backwards seek must replay again."""
    engine, sink, index = _engine_for(Path(good_bag["uri"]), rate=50.0)
    try:
        engine.play()
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        n = len(index.entries)

        # jump to tail
        snap_end = engine.seek(seq=n + 1, play_after=False)
        assert snap_end["finished"] is False  # seeking clears finished flag
        assert snap_end["next_seq"] == n + 1

        # jump back to beginning, play -> new generation delivers everything
        snap_back = engine.seek(seq=1, play_after=True)
        gen = snap_back["generation"]
        assert _wait_until(lambda: engine.position()["finished"], timeout=5)
        msgs = [
            i
            for i in sink.history()
            if i.get("kind") == "message" and i["generation"] == gen
        ]
        assert len(msgs) == n
    finally:
        engine.shutdown()
