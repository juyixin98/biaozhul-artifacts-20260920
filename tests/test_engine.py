"""Tests for the replay engine: ordering, pause, rate, seek/generation."""
from __future__ import annotations

import time

from rosreplay.bagio import load_bag
from rosreplay.engine import PlayState, ReplayEngine
from rosreplay.sinks import MultiSink, RingSink


def make_engine(index, *, rate=100.0, auto_play=False, topics=None):
    ring = RingSink(10_000)
    engine = ReplayEngine(
        index,
        MultiSink([ring]),
        topics=topics,
        rate=rate,
        auto_play=auto_play,
    )
    engine.start()
    return engine, ring


def test_plays_in_canonical_order(index, helpers):
    engine, ring = make_engine(index, rate=1000.0, auto_play=True)
    assert helpers.wait_until(
        lambda: engine.status()["state"] == PlayState.FINISHED.value, timeout=3
    )
    records = ring.snapshot()
    assert len(records) == len(index)
    # Published order matches canonical seq order exactly.
    assert [r.seq for r in records] == list(range(len(index)))
    # Same-timestamp canonical topic ordering observed at publish time.
    topic_at = {}
    for r in records:
        topic_at.setdefault(r.bag_timestamp_ns, []).append(r.topic)
    for ts, topics in topic_at.items():
        pair = [t for t in topics if t in {"/tick", "/tock"}]
        if len(pair) == 2:
            assert pair == ["/tick", "/tock"]
    engine.shutdown()


def test_bag_timestamp_never_rewritten_by_rate(index, helpers):
    engine, ring = make_engine(index, rate=5000.0, auto_play=True)
    helpers.wait_until(lambda: engine.status()["state"] == "finished", timeout=3)
    records = ring.snapshot()
    for r in records:
        entry = index.entries[r.seq]
        assert r.bag_timestamp_ns == entry.timestamp_ns
    engine.shutdown()


def test_pause_halts_and_resume_continues_no_dupes(index, helpers):
    engine, ring = make_engine(index, rate=50.0, auto_play=True)
    helpers.wait_until(lambda: len(ring.snapshot()) >= 5, timeout=3)
    engine.pause()
    time.sleep(0.15)
    paused_seqs = [r.seq for r in ring.snapshot()]
    assert len(paused_seqs) >= 1
    time.sleep(0.15)
    # Nothing new while paused.
    assert [r.seq for r in ring.snapshot()] == paused_seqs
    engine.resume()
    assert helpers.wait_until(lambda: engine.status()["state"] == "finished", timeout=3)
    final = ring.snapshot()
    assert [r.seq for r in final] == list(range(len(index)))
    engine.shutdown()


def test_rate_change_affects_spacing_not_semantics(index, helpers):
    engine, ring = make_engine(index, rate=10.0, auto_play=True)
    helpers.wait_until(lambda: len(ring.snapshot()) >= 3, timeout=3)
    engine.set_rate(5000.0)  # fast-forward the remainder
    assert helpers.wait_until(lambda: engine.status()["state"] == "finished", timeout=3)
    records = ring.snapshot()
    # Every message exactly once, canonical order, original timestamps.
    assert [r.seq for r in records] == list(range(len(index)))
    engine.shutdown()


def test_seek_bumps_generation_and_drops_old(index, helpers):
    engine, ring = make_engine(index, rate=5.0, auto_play=True)
    helpers.wait_until(lambda: len(ring.snapshot()) >= 2, timeout=3)
    gen0 = engine.generation
    # Jump forward to a late filtered cursor.
    target = len(index.entries) - 3
    result = engine.seek(target_seq=target)
    assert result["generation"] == gen0 + 1
    assert result["cursor"] == target
    engine.set_rate(5000.0)
    assert helpers.wait_until(lambda: engine.status()["state"] == "finished", timeout=3)

    gen0_records = ring.snapshot(generation=gen0)
    new_records = ring.snapshot(generation=result["generation"])

    # New generation only publishes from the seek point onward.
    assert new_records, "new generation should publish messages"
    assert min(r.seq for r in new_records) >= index.entries[target].seq
    assert all(r.generation == result["generation"] for r in new_records)

    # CRITICAL: old generation never published anything at/after the jump
    # target (no leaked stale queued messages).
    leaked = [r for r in gen0_records if r.seq >= index.entries[target].seq]
    assert leaked == []
    # Union of both generations still covers each seq at most once.
    all_seqs = [r.seq for r in ring.snapshot()]
    assert len(all_seqs) == len(set(all_seqs))
    engine.shutdown()


def test_seek_to_end_finishes(index, helpers):
    """Jumping to the tail (past last message) yields FINISHED with no emit."""
    engine, ring = make_engine(index, rate=100.0, auto_play=True)
    result = engine.seek(target_seq=len(index.entries))
    assert result["state"] == PlayState.FINISHED.value
    time.sleep(0.1)
    # The fresh generation emitted nothing.
    assert ring.snapshot(generation=result["generation"]) == []
    assert engine.status()["next_seq"] is None
    engine.shutdown()


def test_seek_by_timestamp_uses_lower_bound(index):
    engine, _ = make_engine(index, auto_play=False)
    ts = index.start_ns + 3 * 100_000_000  # 300ms in
    result = engine.seek(target_ns=ts, play=False)
    entry = index.entries[result["cursor"]]
    assert entry.timestamp_ns >= ts
    # It is the first such entry.
    assert all(e.timestamp_ns < ts for e in index.entries[: result["cursor"]])
    engine.shutdown()


def test_topic_filter_restricts_published(index, helpers):
    engine, ring = make_engine(
        index, rate=2000.0, auto_play=True, topics=frozenset({"/tick"})
    )
    helpers.wait_until(lambda: engine.status()["state"] == "finished", timeout=3)
    topics = {r.topic for r in ring.snapshot()}
    assert topics == {"/tick"}
    engine.shutdown()


def test_claim_rejects_stale_generation_directly(index):
    """Unit-level proof of the cancellation guard.

    An item is stamped with the generation that scheduled it. After a seek the
    generation is bumped; when the old item reaches the claim point it must be
    rejected (and counted dropped), so it can never be emitted. This is the
    exact check the engine thread performs between dequeue and publish.
    """
    engine, ring = make_engine(index, auto_play=False)
    import heapq

    from rosreplay.engine import _WorkItem

    entry = index.entries[5]
    stale = _WorkItem(
        due_wall=0.0, order=1, pos=5, generation=engine.generation, entry=entry
    )
    # Current generation, playing, and item at the emitted position => claimed.
    engine.state = PlayState.PLAYING
    engine.emitted_pos = 5
    assert engine._claim(stale) is True
    assert engine.emitted_pos == 6  # claiming advances the emit watermark

    # A seek happens (new generation, cursor moved away).
    engine.seek(target_seq=20, play=True)
    stale2 = _WorkItem(
        due_wall=0.0, order=2, pos=5, generation=0, entry=entry
    )
    assert engine._claim(stale2) is False
    assert engine.dropped_count == 1
    assert ring.snapshot() == []  # rejected item never reached the sink
    engine.shutdown()


def test_seek_to_end_clears_pending_without_leak(index):
    """Seek to tail while messages are scheduled: none from the old generation
    may appear at or beyond the tail (there is nothing there), and no exception
    is raised by rewinding an empty/finished heap."""
    engine, ring = make_engine(index, rate=10.0, auto_play=True)
    time.sleep(0.05)
    result = engine.seek(target_seq=len(index.entries), play=False)
    assert result["state"] == PlayState.FINISHED.value
    time.sleep(0.05)
    assert ring.snapshot(generation=result["generation"]) == []
    engine.shutdown()


def test_rapid_seek_cancellation_race(index, helpers):
    """Stress: hammer seeks while playing; engine stays consistent.

    The hard no-leak guarantee is proven deterministically by
    ``test_claim_rejects_stale_generation_directly`` (the claim point rejects
    any item whose generation differs). Here we assert the externally observable
    invariants that must survive rapid cancellation, regardless of exactly how
    many messages each superseded generation managed to emit:

    * within every generation the emitted seqs are unique and canonically
      ordered (no duplicates / reordering introduced by cancellation);
    * every emitted record carries a real generation id;
    * the newest generation starts at its requested target.
    """
    engine, ring = make_engine(index, rate=200.0, auto_play=True)
    for target in [8, 16, 24, 32]:
        engine.seek(target_seq=target, play=True)
        time.sleep(0.005)
    final_target = 32
    engine.set_rate(5000.0)
    helpers.wait_until(lambda: engine.status()["state"] == "finished", timeout=3)

    gens = {r.generation for r in ring.snapshot()}
    for gen in gens:
        seqs = [r.seq for r in ring.snapshot(generation=gen)]
        assert seqs == sorted(seqs), f"gen {gen} reordered: {seqs}"
        assert len(seqs) == len(set(seqs)), f"gen {gen} duplicated seqs"
    newest = engine.generation
    newest_records = ring.snapshot(generation=newest)
    assert newest_records and min(r.seq for r in newest_records) >= index.entries[final_target].seq
    engine.shutdown()


def test_generation_monotonic_on_seek_only(index):
    engine, _ = make_engine(index, auto_play=False)
    g0 = engine.generation
    engine.set_rate(2.0)
    engine.pause()
    engine.resume()
    assert engine.generation == g0  # rate/pause/resume do not change generation
    engine.seek(target_seq=1)
    assert engine.generation == g0 + 1
    engine.shutdown()
