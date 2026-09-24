"""Deterministic replay engine with generation-sealed cancellation.

Timing model
------------
The engine keeps a *virtual playback clock* mapping bag timestamps onto wall
time. Let ``v(now)`` be the bag timestamp currently "due" and ``rate`` the
speed multiplier:

* while playing, ``v(now) = v(anchor) + rate*(now - anchor_wall)``
* pausing freezes ``v``; resuming re-anchors at the frozen value
* changing the rate freezes ``v`` and immediately re-anchors with the new
  rate -- rates only ever rescale future wall waits, the recorded message
  timestamps are never rewritten
* seeking freezes the clock at the target timestamp and re-anchors there

Messages are delivered when the virtual clock reaches their recorded
timestamp; the **payload timestamp field always carries the original bag
timestamp**, so speed changes cannot alter message time semantics.

Generation model
----------------
Every seek/stop increments ``generation``. The scheduler thread holds the
state lock while it (a) checks that the message's generation is current and
(b) hands the payload to the sink. A control command that wins the lock
first therefore makes every still-queued older-generation message fail that
check before it can touch the sink: *old generations never publish*.
"""
from __future__ import annotations

import base64
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .bagstore import BagIndex, IndexEntry


class SeekOutOfRangeError(ValueError):
    pass


@dataclass
class EngineState:
    generation: int = 0
    playing: bool = False
    rate: float = 1.0
    # Position of the next message to deliver (1..N+1). N+1 == finished.
    next_seq: int = 1
    finished: bool = False
    topic_filter: Optional[tuple[str, ...]] = None  # None == all topics
    # Virtual clock anchors: (virtual_ns, wall_seconds)
    anchor_virtual_ns: Optional[int] = None
    anchor_wall: Optional[float] = None
    published_count: int = 0
    # cumulative envelope counters per generation, for subscriber assertions
    generation_published: int = 0
    created_at: float = field(default_factory=time.time)


class ReplayEngine:
    """One engine per session. All public methods are thread-safe."""

    def __init__(
        self,
        index: BagIndex,
        sink: Any,
        *,
        tick_seconds: float = 0.005,
        topic_filter: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        on_generation_change: Optional[Callable[[int], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        payloads: Optional[dict[int, bytes]] = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self._index = index
        self._sink = sink
        self._tick = tick_seconds
        self._clock = clock
        self._on_generation_change = on_generation_change
        # seq -> recorded CDR payload. Populated by the service from the same
        # reader pass that builds the index (small local test bags).
        self._payloads: dict[int, bytes] = payloads or {}

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._state = EngineState(
            topic_filter=tuple(topic_filter) if topic_filter else None
        )
        self._stop_thread = False
        self._thread = threading.Thread(
            target=self._run, name=f"replay-{self.session_id}", daemon=True
        )
        self._thread.start()

    # ------------------------------------------------------------------ utils

    @property
    def index(self) -> BagIndex:
        return self._index

    def _entries(self) -> tuple[IndexEntry, ...]:
        return self._index.entries

    def _topic_allowed(self, topic: str) -> bool:
        f = self._state.topic_filter
        return f is None or topic in f

    def _virtual_now_ns(self) -> int:
        s = self._state
        if not s.playing or s.anchor_virtual_ns is None:
            # Frozen virtual time = timestamp of the next due message minus an
            # epsilon; represented explicitly as anchor when paused.
            return s.anchor_virtual_ns if s.anchor_virtual_ns is not None else self._index.starting_time_ns
        elapsed = self._clock() - (s.anchor_wall or 0.0)
        return int(s.anchor_virtual_ns + s.rate * elapsed * 1e9)

    def _bump_generation(self, reason: str) -> int:
        s = self._state
        s.generation += 1
        s.generation_published = 0
        s.finished = False
        self._emit_event_sync(
            {
                "kind": "generation",
                "reason": reason,
                "generation": s.generation,
                "next_seq": s.next_seq,
                "playing": s.playing,
            }
        )
        if self._on_generation_change:
            try:
                self._on_generation_change(s.generation)
            except Exception:
                pass
        self._cond.notify_all()
        return s.generation

    # ------------------------------------------------------------- envelopes

    def _message_envelope(
        self, entry: IndexEntry, generation: int, gen_seq: int
    ) -> dict[str, Any]:
        return {
            "kind": "message",
            "session_id": self.session_id,
            "generation": generation,
            "generation_seq": gen_seq,
            "seq": entry.seq,
            "topic": entry.topic,
            "type": entry.topic_type,
            "timestamp_ns": entry.timestamp_ns,  # ORIGINAL bag timestamp
            "data_length": entry.data_length,
            "data_sha256": entry.data_sha256,
            "data_b64": base64.b64encode(self._payloads[entry.seq]).decode("ascii"),
        }

    def _emit_event_sync(self, event: dict[str, Any]) -> None:
        """Emit an event via the sink while the caller holds the lock.

        Events are generation-tagged too, so subscribers can order them.
        """
        payload = {
            "session_id": self.session_id,
            "generation": self._state.generation,
            **event,
        }
        self._sink.publish_event(payload)

    # ------------------------------------------------------------ public API

    def play(self) -> dict[str, Any]:
        with self._cond:
            s = self._state
            if s.finished:
                return self.snapshot()
            if not s.playing:
                s.anchor_virtual_ns = self._paused_virtual_ns()
                s.anchor_wall = self._clock()
                s.playing = True
                self._emit_event_sync(
                    {
                        "kind": "transport",
                        "state": "playing",
                        "rate": s.rate,
                        "generation": s.generation,
                    }
                )
            self._cond.notify_all()
            return self.snapshot()

    def pause(self) -> dict[str, Any]:
        with self._cond:
            s = self._state
            if s.playing:
                frozen = self._virtual_now_ns()
                s.playing = False
                s.anchor_virtual_ns = frozen
                s.anchor_wall = None
                self._emit_event_sync(
                    {
                        "kind": "transport",
                        "state": "paused",
                        "rate": s.rate,
                        "generation": s.generation,
                    }
                )
            self._cond.notify_all()
            return self.snapshot()

    def set_rate(self, rate: float) -> dict[str, Any]:
        if not (0.01 <= rate <= 100.0):
            raise ValueError("rate must be within [0.01, 100.0]")
        with self._cond:
            s = self._state
            frozen = self._virtual_now_ns()
            s.rate = float(rate)
            if s.playing:
                s.anchor_virtual_ns = frozen
                s.anchor_wall = self._clock()
            else:
                s.anchor_virtual_ns = frozen
            self._emit_event_sync(
                {
                    "kind": "rate",
                    "rate": s.rate,
                    "generation": s.generation,
                }
            )
            self._cond.notify_all()
            return self.snapshot()

    def seek(
        self,
        *,
        seq: Optional[int] = None,
        timestamp_ns: Optional[int] = None,
        ratio: Optional[float] = None,
        play_after: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Jump to a position. Always opens a new generation."""
        entries = self._entries()
        n = len(entries)
        if seq is not None:
            target = int(seq)
            mode = "seq"
        elif timestamp_ns is not None:
            target = int(timestamp_ns)
            mode = "timestamp_ns"
        elif ratio is not None:
            if not (0.0 <= float(ratio) <= 1.0):
                raise SeekOutOfRangeError("ratio must be within [0.0, 1.0]")
            target = float(ratio)
            mode = "ratio"
        else:
            raise ValueError("one of seq/timestamp_ns/ratio is required")

        with self._cond:
            s = self._state
            if mode == "seq":
                if not (1 <= target <= n + 1):
                    raise SeekOutOfRangeError(f"seq must be within [1, {n + 1}]")
                next_seq = target
                target_ts = entries[target - 1].timestamp_ns if target <= n else (
                    entries[-1].timestamp_ns if n else self._index.starting_time_ns
                )
            elif mode == "timestamp_ns":
                if n == 0:
                    next_seq, target_ts = 1, self._index.starting_time_ns
                else:
                    if target < entries[0].timestamp_ns or target > entries[-1].timestamp_ns:
                        raise SeekOutOfRangeError(
                            f"timestamp {target} outside bag "
                            f"[{entries[0].timestamp_ns}, {entries[-1].timestamp_ns}]"
                        )
                    # First message with timestamp >= target (lower bound).
                    lo, hi = 0, n
                    while lo < hi:
                        mid = (lo + hi) // 2
                        if entries[mid].timestamp_ns < target:
                            lo = mid + 1
                        else:
                            hi = mid
                    next_seq = lo + 1
                    target_ts = target
            else:  # ratio
                if n == 0:
                    next_seq, target_ts = 1, self._index.starting_time_ns
                elif target >= 1.0:
                    next_seq, target_ts = n + 1, entries[-1].timestamp_ns
                else:
                    idx = min(n - 1, int(target * n))
                    next_seq = idx + 1
                    target_ts = entries[idx].timestamp_ns

            # Decide play state BEFORE anchoring the virtual clock.
            if play_after is True:
                new_playing = True
            elif play_after is False:
                new_playing = False
            else:
                new_playing = s.playing

            s.next_seq = next_seq
            s.finished = False
            # Anchor the virtual clock exactly at the jump target. When
            # playing, anchor wall=now, so the first message (timestamp ==
            # target) is due immediately and every later one waits exactly
            # its recorded gap / rate -- nothing is flushed in a burst.
            s.anchor_virtual_ns = target_ts
            s.playing = new_playing
            s.anchor_wall = self._clock() if new_playing else None
            gen = self._bump_generation(f"seek:{mode}")
            self._emit_event_sync(
                {
                    "kind": "seek",
                    "mode": mode,
                    "target": target,
                    "next_seq": next_seq,
                    "timestamp_ns": target_ts,
                    "playing": s.playing,
                    "generation": gen,
                }
            )
            self._cond.notify_all()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        """Pause and invalidate everything queued (new generation).

        The play position is kept; a subsequent ``play`` continues from the
        current position under the new generation, and ``seek`` can move it.
        """
        with self._cond:
            s = self._state
            was_playing = s.playing
            s.playing = False
            frozen = self._virtual_now_ns()
            s.anchor_virtual_ns = frozen
            s.anchor_wall = None
            gen = self._bump_generation("stop")
            self._emit_event_sync(
                {
                    "kind": "transport",
                    "state": "stopped",
                    "rate": s.rate,
                    "was_playing": was_playing,
                    "next_seq": s.next_seq,
                    "generation": gen,
                }
            )
            self._cond.notify_all()
            return self.snapshot()

    def set_topic_filter(self, topics: Optional[list[str]]) -> dict[str, Any]:
        with self._cond:
            s = self._state
            if topics is None:
                s.topic_filter = None
            else:
                unknown = sorted({t for t in topics} - set(self._index.topic_names()))
                if unknown:
                    raise ValueError(f"topics not in bag: {unknown}")
                s.topic_filter = tuple(topics)
            self._emit_event_sync(
                {
                    "kind": "filter",
                    "topics": list(s.topic_filter) if s.topic_filter else None,
                    "generation": s.generation,
                }
            )
            return self.snapshot()

    def _paused_virtual_ns(self) -> int:
        """Virtual time to anchor from when (re)starting play."""
        s = self._state
        entries = self._entries()
        n = len(entries)
        if n == 0:
            return self._index.starting_time_ns
        if s.next_seq > n:
            return entries[-1].timestamp_ns
        # Anchor at the next due message so it is delivered immediately.
        return entries[s.next_seq - 1].timestamp_ns

    # ------------------------------------------------------------ snapshots

    def position(self) -> dict[str, Any]:
        with self._lock:
            return self._position_locked()

    def _position_locked(self) -> dict[str, Any]:
        s = self._state
        entries = self._entries()
        n = len(entries)
        seq = min(s.next_seq, n)
        current = entries[seq - 1] if 1 <= seq <= n else None
        last = entries[s.next_seq - 2] if 1 <= s.next_seq - 1 <= n else None
        return {
            "generation": s.generation,
            "playing": s.playing,
            "finished": s.finished,
            "rate": s.rate,
            "next_seq": s.next_seq,
            "last_seq": last.seq if last else None,
            "last_timestamp_ns": last.timestamp_ns if last else None,
            "current_timestamp_ns": current.timestamp_ns if current else None,
            "virtual_time_ns": self._virtual_now_ns(),
            "published_count": s.published_count,
            "generation_published": s.generation_published,
            "topic_filter": list(s.topic_filter) if s.topic_filter else None,
            "total_messages": n,
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            pos = self._position_locked()
        return {"session_id": self.session_id, **pos}

    def checkpoint_position(self) -> dict[str, Any]:
        """Stable position record used by checkpoints."""
        with self._lock:
            s = self._state
            return {
                "next_seq": s.next_seq,
                "rate": s.rate,
                "playing": False,  # checkpoints always resume paused
                "topic_filter": list(s.topic_filter) if s.topic_filter else None,
                "last_generation": s.generation,
            }

    def restore_position(self, pos: dict[str, Any]) -> None:
        with self._cond:
            s = self._state
            n = len(self._entries())
            next_seq = int(pos["next_seq"])
            if not (1 <= next_seq <= n + 1):
                raise SeekOutOfRangeError(f"checkpoint next_seq {next_seq} out of range")
            s.next_seq = next_seq
            s.rate = float(pos.get("rate", 1.0))
            s.playing = False
            s.finished = next_seq > n
            s.topic_filter = (
                tuple(pos["topic_filter"]) if pos.get("topic_filter") else None
            )
            anchor_ts = (
                self._entries()[next_seq - 1].timestamp_ns
                if next_seq <= n
                else (self._entries()[-1].timestamp_ns if n else self._index.starting_time_ns)
            )
            s.anchor_virtual_ns = anchor_ts
            s.anchor_wall = None
            gen = self._bump_generation("restore")
            self._emit_event_sync(
                {
                    "kind": "restore",
                    "next_seq": next_seq,
                    "rate": s.rate,
                    "generation": gen,
                }
            )
            self._cond.notify_all()

    # --------------------------------------------------------------- thread

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._cond:
            self._stop_thread = True
            self._cond.notify_all()
        self._thread.join(timeout=timeout)
        try:
            self._sink.close()
        except Exception:
            pass

    def _wait_deadline(self, deadline_wall: float) -> bool:
        """Sleep until deadline/state change/stop. Returns False on shutdown."""
        while not self._stop_thread:
            now = self._clock()
            if now >= deadline_wall:
                return True
            self._cond.wait(timeout=min(self._tick, deadline_wall - now))
        return False

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stop_thread:
                    return
                s = self._state
                entries = self._entries()
                n = len(entries)

                if s.finished or s.next_seq > n:
                    if not s.finished and s.next_seq > n:
                        s.finished = True
                        self._emit_event_sync(
                            {
                                "kind": "end_of_bag",
                                "generation": s.generation,
                                "last_seq": n,
                                "last_timestamp_ns": entries[-1].timestamp_ns if n else None,
                            }
                        )
                        s.playing = False
                    self._cond.wait(timeout=self._tick)
                    continue

                if not s.playing:
                    self._cond.wait(timeout=self._tick)
                    continue

                # Find the next filtered entry at/after the playhead.
                idx = s.next_seq - 1
                while idx < n and not self._topic_allowed(entries[idx].topic):
                    idx += 1

                if idx >= n:
                    # Filtered stream exhausted: finish.
                    s.next_seq = n + 1
                    continue

                entry = entries[idx]
                virtual = self._virtual_now_ns()
                if entry.timestamp_ns > virtual:
                    wait_s = (entry.timestamp_ns - virtual) / 1e9 / max(s.rate, 1e-9)
                    deadline = self._clock() + max(0.0, wait_s)
                    gen_before_wait = s.generation
                    playing_before = s.playing
                    if not self._wait_deadline(deadline):
                        return
                    # A seek/stop/rate change can wake the condition before
                    # the deadline. The playhead position captured above may
                    # now belong to a dead generation; drop it and re-evaluate
                    # from the top under the current generation.
                    if (
                        s.generation != gen_before_wait
                        or not s.playing
                        or s.playing != playing_before
                    ):
                        continue

                # Due now. Snapshot the credentials checked at publish time.
                # generation/gen_seq are read and consumed atomically: the
                # condition lock is held across the sink call, so a seek or
                # stop that increments generation cannot interleave with this
                # publish. Any older-generation item queued before the jump
                # fails to reach here (its iteration restarts above).
                generation = s.generation
                gen_seq = s.generation_published + 1
                envelope = self._message_envelope(entry, generation, gen_seq)
                self._sink.publish_message(envelope)
                # Advance the stable playhead past idx regardless of the
                # filter: seq numbers count every message in the bag.
                s.generation_published = gen_seq
                s.published_count += 1
                s.next_seq = idx + 2

    # ------------------------------------------------------------------ #


# Message payloads are passed in explicitly at construction time; there is no
# global mutable provider registry.
