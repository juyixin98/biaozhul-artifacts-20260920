"""Stress test: old generations must never publish, even under races.

The scheduler thread publishes messages continuously while other threads fire
seek/stop/set_rate/pause commands as fast as they can. After the storm, every
delivered envelope is checked: an envelope carrying generation ``g`` must be
consistent with the active generation when it was published, messages of a
superseded generation cannot appear after that generation's final event, and
each generation's sequence counters must be dense and ordered.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from tests.conftest import requires_ros


def _storm_engine(bag_uri, *, rate, tick=0.001):
    from app import bagstore
    from app.playback import ReplayEngine
    from app.sinks import LoopbackSink

    index, payloads = bagstore.index_bag_with_payloads(bag_uri)
    sink = LoopbackSink()
    engine = ReplayEngine(
        index, sink, tick_seconds=tick, payloads=payloads, session_id="storm"
    )
    engine.set_rate(rate)
    return engine, sink, index


@requires_ros
@pytest.mark.slow
@pytest.mark.parametrize("iterations", [300])
def test_old_generation_messages_never_published(good_bag, iterations):
    engine, sink, index = _storm_engine(Path(good_bag["uri"]), rate=80.0)
    n = len(index.entries)
    stop_flag = threading.Event()
    errors: list[str] = []

    engine.play()

    def storm_seek():
        rng_state = 0
        while not stop_flag.is_set():
            # pseudo-random but deterministic jumps
            rng_state = (rng_state * 1103515245 + 12345) & 0x7FFFFFFF
            target = 1 + rng_state % n
            choice = rng_state % 4
            try:
                if choice == 0:
                    engine.seek(seq=target, play_after=True)
                elif choice == 1:
                    engine.seek(ratio=(rng_state % 1000) / 1000.0, play_after=True)
                elif choice == 2:
                    engine.stop()
                    engine.play()
                else:
                    engine.set_rate(1.0 + (rng_state % 50))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"storm op raised: {exc!r}")
            time.sleep(0)  # yield

    t = threading.Thread(target=storm_seek)
    t.start()
    time.sleep(2.5)
    stop_flag.set()
    t.join(timeout=2)
    engine.pause()

    all_items = sink.history()
    messages = [i for i in all_items if i.get("kind") == "message"]
    events = [i for i in all_items if i.get("kind") != "message"]

    # ----- invariant 1: generation boundaries are monotonic in delivery -----
    #
    # A message of generation g can only be delivered before the generation
    # event that opens g+1. I.e. once any item with generation > g appears,
    # no later message may claim generation g.
    max_gen_seen = -1
    # events and messages share a single ordered history (history_id)
    for item in all_items:
        gen = item.get("generation")
        if gen is None:
            continue
        kind = item.get("kind")
        if kind == "message":
            if gen < max_gen_seen:
                errors.append(
                    f"STALE: message gen {gen} delivered after gen {max_gen_seen} "
                    f"(seq={item.get('seq')})"
                )
        else:
            if gen > max_gen_seen:
                max_gen_seen = gen

    # ----- invariant 2: within a generation, seq strictly increases ---------
    #
    # generation_seq counts messages in THIS generation starting at 1, but a
    # generation opened by a seek to the middle may legitimately begin at a
    # generation_seq > 1 (the 1..k-1 entries were delivered in that same
    # generation before it is observable to this late check). The exact
    # guarantee is therefore: between two consecutive observed messages of
    # the same generation, generation_seq increases by exactly 1, stable seq
    # strictly increases, and timestamps never go backwards.
    per_gen_last_seq: dict[int, int] = {}
    per_gen_last_gen_seq: dict[int, int] = {}
    per_gen_last_ts: dict[int, int] = {}
    for m in messages:
        g = m["generation"]
        if m["seq"] <= per_gen_last_seq.get(g, 0):
            errors.append(
                f"g{g}: seq went backwards {per_gen_last_seq.get(g)} -> {m['seq']}"
            )
        per_gen_last_seq[g] = m["seq"]
        if g in per_gen_last_gen_seq:
            if m["generation_seq"] != per_gen_last_gen_seq[g] + 1:
                errors.append(
                    f"g{g}: generation_seq gap: got {m['generation_seq']} "
                    f"after {per_gen_last_gen_seq[g]}"
                )
        else:
            if m["generation_seq"] < 1:
                errors.append(
                    f"g{g}: first observed generation_seq must be >= 1, "
                    f"got {m['generation_seq']}"
                )
        per_gen_last_gen_seq[g] = m["generation_seq"]
        if m["timestamp_ns"] < per_gen_last_ts.get(g, -1):
            errors.append(
                f"g{g}: timestamp went backwards {per_gen_last_ts.get(g)} -> {m['timestamp_ns']}"
            )
        per_gen_last_ts[g] = m["timestamp_ns"]

    # ----- invariant 3: a seek target must be honoured for that generation --
    # Every message tagged with the generation opened by a seek has ts >= that
    # seek's target timestamp.
    seek_targets: dict[int, int] = {}
    for e in events:
        if e.get("kind") == "seek":
            seek_targets[e["generation"]] = e["timestamp_ns"]
    for m in messages:
        target = seek_targets.get(m["generation"])
        if target is not None and m["timestamp_ns"] < target:
            errors.append(
                f"g{m['generation']}: message ts {m['timestamp_ns']} "
                f"precedes seek target {target}"
            )

    engine.shutdown()

    assert not errors, "invariant failures:\n" + "\n".join(errors[:20])
    assert messages, "storm should have delivered some messages"
    assert max_gen_seen >= 5, f"expected many generations in storm, got {max_gen_seen}"


@requires_ros
def test_seek_during_wait_never_publishes_stale_generation(good_bag):
    """Deterministic regression for "woken early by seek".

    Start playing at 1x on a bag whose inter-message gap is larger than the
    test window, then -- while the scheduler is blocked in its deadline wait
    -- jump to the end and immediately back to the start under a new
    generation. Every message eventually delivered must belong to the final
    generation; no message from the generation whose wait was interrupted may
    appear AFTER the generation event that superseded it.
    """
    import time

    engine, sink, index = _storm_engine(Path(good_bag["uri"]), rate=1.0, tick=0.001)
    n = len(index.entries)
    try:
        engine.play()
        # The scheduler is now sleeping until the next message is due.
        time.sleep(0.005)
        # Interrupt that wait with an end-jump (new gen), then back-to-start.
        engine.seek(seq=n + 1, play_after=False)
        final_gen = engine.seek(seq=1, play_after=True)["generation"]
        time.sleep(0.3)
        engine.set_rate(80.0)  # drain quickly
        deadline = time.time() + 5
        while time.time() < deadline and not engine.position()["finished"]:
            time.sleep(0.01)

        items = sink.history()
        max_gen_before_message = -1
        stale = []
        for it in items:
            if it.get("kind") != "message":
                max_gen_before_message = max(
                    max_gen_before_message, it.get("generation", -1)
                )
            elif it["generation"] < max_gen_before_message:
                stale.append((it["generation"], max_gen_before_message, it["seq"]))
        assert not stale, f"stale messages after supersede: {stale[:5]}"
    finally:
        engine.shutdown()


@requires_ros
def test_stop_then_fast_seek_race_no_old_messages(good_bag):
    """stop() immediately followed by seek(): neither gen's tail may leak."""
    engine, sink, index = _storm_engine(Path(good_bag["uri"]), rate=100.0)
    n = len(index.entries)
    errors: list[str] = []

    for round_i in range(60):
        engine.play()
        # tiny bit of playback
        engine.seek(seq=1 + (round_i * 3) % max(1, n - 2), play_after=True)
        engine.stop()
        engine.seek(seq=1, play_after=False)
        engine.play()
    engine.pause()
    time.sleep(0.05)

    items = sink.history()
    max_gen = -1
    for item in items:
        gen = item.get("generation", -1)
        if item.get("kind") != "message":
            max_gen = max(max_gen, gen)
            continue
        if gen < max_gen:
            errors.append(f"stale gen {gen} after {max_gen}")

    engine.shutdown()
    assert not errors
