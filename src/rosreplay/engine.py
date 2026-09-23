"""The replay scheduler.

A single session owns one :class:`ReplayEngine` and one engine thread. The
engine walks a precomputed, deterministically ordered message index and hands
records to the output sinks with wall-clock spacing derived from the *original*
bag timestamps.

Two positions and the generation rule
-------------------------------------
Features (pause / set-rate / seek) are requested from FastAPI worker threads and
consumed by the engine thread. Shared state lives behind ``_lock``.

* ``emitted_pos``   : position of the next message that must be *published*
* ``scheduled_pos`` : position of the next message to put in the timing heap
* ``state``         : PLAYING / PAUSED / FINISHED
* ``rate``          : playback speed (changes do not alter bag timestamps)
* ``generation``    : bumped on every seek / restart

Scheduling reads from ``scheduled_pos``; publishing only ever consumes the item
at ``emitted_pos`` and advances it. Every scheduled heap item is stamped with
the generation that scheduled it. Before an item is emitted the engine
re-checks the generation under the lock: if a newer generation exists, the item
is discarded and counted as dropped. That is the hard guarantee behind "a jump
creates a new generation and queued messages from the old generation must never
be published" — even messages already dequeued and microseconds from going out
cannot leak after a seek.

Pause and rate change do *not* move ``emitted_pos`` and do *not* bump the
generation; they merely flush the timing heap and reset ``scheduled_pos`` back
to ``emitted_pos`` so the unemitted remainder is rescheduled. Seek moves
``emitted_pos`` (and therefore ``scheduled_pos``) and invalidates the
generation.

Time semantics
--------------
When playing we anchor the first scheduled message to wall-clock now and compute
each subsequent due time as

    due = anchor + (bag_ts(pos) - bag_ts(anchor_pos)) / rate

so the *relative* timing of messages is preserved exactly while only the
playback clock is scaled. The carried ``bag_timestamp_ns`` always equals the
original bag timestamp regardless of rate.
"""
from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .bagio import BagIndex, IndexEntry
from .sinks import MultiSink, PublishedRecord

# Wall-clock granularity at which the engine wakes to re-evaluate. Sleeps are
# interruptible by the condition variable so pause/seek react immediately.
_TICK = 0.01
# At most this many messages are scheduled ahead. Small enough that a seek
# cannot leave much stale work, large enough that bursts at one timestamp are
# always fully scheduled as a contiguous ordered block.
_LOOKAHEAD = 64


class PlayState(str, Enum):
    PLAYING = "playing"
    PAUSED = "paused"
    FINISHED = "finished"


@dataclass(order=True)
class _WorkItem:
    due_wall: float
    order: int  # tie-break so IndexEntry isn't compared
    pos: int = field(compare=False)  # position within the filtered list
    generation: int = field(compare=False)
    entry: IndexEntry = field(compare=False)


def _now_ns() -> int:
    return time.time_ns()


class ReplayEngine:
    def __init__(
        self,
        index: BagIndex,
        sink: MultiSink,
        topics: frozenset[str] | None = None,
        rate: float = 1.0,
        auto_play: bool = True,
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self._index = index
        self._sink = sink
        self._all = index.filtered_entries(topics)
        self._topics = topics
        self._time = time_func

        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._heap: list[_WorkItem] = []
        self._order_seq = 0

        self.generation = 0
        self.rate = self._validate_rate(rate)
        self.emitted_pos = 0  # next message to publish
        self.scheduled_pos = 0  # next message to schedule
        self.state = PlayState.PLAYING if auto_play else PlayState.PAUSED
        self.anchor_wall: float | None = None
        self.anchor_pos: int | None = None
        self.published_count = 0
        self.dropped_count = 0
        self.finished_ns: int | None = None

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _validate_rate(rate: float) -> float:
        rate = float(rate)
        if rate <= 0:
            raise ValueError("rate must be > 0 (use pause to halt)")
        return rate

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="replay-engine", daemon=True
        )
        self._thread.start()

    def shutdown(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        t, self._thread = self._thread, None
        if t is not None:
            t.join(timeout=timeout)

    # ------------------------------------------------------------- public API
    def _flush_timing_locked(self) -> None:
        """Discard scheduled (unemitted) work and reschedule from emitted_pos.

        Used by pause/rate. Never moves emitted_pos, never changes generation,
        so already-published messages are neither repeated nor invalidated.
        """
        self._heap.clear()
        self.scheduled_pos = self.emitted_pos
        self.anchor_wall = None
        self.anchor_pos = None

    def pause(self) -> None:
        with self._wake:
            if self.state is PlayState.PLAYING:
                self.state = PlayState.PAUSED
                self._flush_timing_locked()
            self._wake.notify_all()

    def resume(self) -> None:
        with self._wake:
            if self.state is PlayState.PAUSED:
                if self.emitted_pos >= len(self._all):
                    self.state = PlayState.FINISHED
                    self.finished_ns = _now_ns()
                else:
                    self.state = PlayState.PLAYING
                    self._flush_timing_locked()
            self._wake.notify_all()

    def set_rate(self, rate: float) -> float:
        rate = self._validate_rate(rate)
        with self._wake:
            self.rate = rate
            # Reschedule the unemitted remainder under the new rate, whether
            # currently playing or paused. emitted_pos is preserved.
            self._flush_timing_locked()
            self._wake.notify_all()
        return rate

    def seek(
        self,
        target_seq: int | None = None,
        target_ns: int | None = None,
        *,
        play: bool | None = None,
    ) -> dict:
        """Jump to a position. Invalidates the current generation.

        Either a filtered-list position or a bag timestamp (lower bound) may be
        given. ``play`` optionally forces playing/paused after the jump; if
        ``None`` the prior play/pause state is preserved.
        """
        new_pos = self._resolve_position(target_seq, target_ns)
        with self._wake:
            old_gen = self.generation
            self.generation += 1
            self.emitted_pos = new_pos
            self.scheduled_pos = new_pos
            self._heap.clear()
            self.anchor_wall = None
            self.anchor_pos = None
            at_end = new_pos >= len(self._all)
            if play is True:
                self.state = PlayState.FINISHED if at_end else PlayState.PLAYING
            elif play is False:
                self.state = PlayState.FINISHED if at_end else PlayState.PAUSED
            elif at_end:
                self.state = PlayState.FINISHED
            if self.state is PlayState.FINISHED:
                self.finished_ns = _now_ns()
            else:
                self.finished_ns = None
            self._wake.notify_all()
        return {
            "old_generation": old_gen,
            "generation": self.generation,
            "cursor": new_pos,
            "state": self.state.value,
        }

    def _resolve_position(self, target_seq: int | None, target_ns: int | None) -> int:
        n = len(self._all)
        if target_seq is not None:
            if target_seq < 0:
                target_seq = 0
            if target_seq > n:
                target_seq = n
            return target_seq
        if target_ns is not None:
            lo, hi = 0, n  # lower_bound: first entry with timestamp >= target
            while lo < hi:
                mid = (lo + hi) // 2
                if self._all[mid].timestamp_ns < target_ns:
                    lo = mid + 1
                else:
                    hi = mid
            return lo
        raise ValueError("seek requires target_seq or target_ns")

    def status(self) -> dict:
        with self._lock:
            cur = self._all[self.emitted_pos] if self.emitted_pos < len(self._all) else None
            return {
                "state": self.state.value,
                "generation": self.generation,
                "rate": self.rate,
                "cursor": self.emitted_pos,
                "total_filtered": len(self._all),
                "total_index": len(self._index),
                "next_seq": cur.seq if cur else None,
                "next_timestamp_ns": cur.timestamp_ns if cur else None,
                "published_count": self.published_count,
                "dropped_count": self.dropped_count,
                "start_ns": self._index.start_ns,
                "end_ns": self._index.end_ns,
                "topics": sorted(self._topics) if self._topics else None,
            }

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._wake:
                if self.state is not PlayState.PLAYING:
                    self._wake.wait(timeout=_TICK)
                    continue
                self._refill_locked()
                if not self._heap:
                    if self.emitted_pos >= len(self._all):
                        self.state = PlayState.FINISHED
                        self.finished_ns = _now_ns()
                        self._wake.notify_all()
                        continue
                    self._wake.wait(timeout=_TICK)
                    continue

                item = self._heap[0]
                now = self._time()
                if item.due_wall > now:
                    self._wake.wait(timeout=min(_TICK, item.due_wall - now))
                    continue
                heapq.heappop(self._heap)

            # Emit outside the lock. Generation is re-checked atomically here;
            # this is the cancellation point that prevents stale generations.
            if not self._claim(item):
                continue
            self._emit(item)

    def _refill_locked(self) -> None:
        """Schedule up to ``_LOOKAHEAD`` unemitted messages ahead."""
        if self.state is not PlayState.PLAYING:
            return
        if self.scheduled_pos >= len(self._all):
            return
        first = self._all[self.scheduled_pos]
        if self.anchor_wall is None:
            self.anchor_wall = self._time()
            self.anchor_pos = self.scheduled_pos
        assert self.anchor_pos is not None
        anchor_bag_ns = self._all[self.anchor_pos].timestamp_ns
        rate = self.rate
        scheduled = 0
        while self.scheduled_pos < len(self._all) and scheduled < _LOOKAHEAD:
            entry = self._all[self.scheduled_pos]
            delta_ns = entry.timestamp_ns - anchor_bag_ns
            due = self.anchor_wall + (delta_ns / 1e9) / rate
            # Messages sharing one due time are pushed as a block; heap `order`
            # preserves their canonical sequence order at equal timestamps.
            self._order_seq += 1
            heapq.heappush(
                self._heap,
                _WorkItem(
                    due_wall=due,
                    order=self._order_seq,
                    pos=self.scheduled_pos,
                    generation=self.generation,
                    entry=entry,
                ),
            )
            self.scheduled_pos += 1
            scheduled += 1

    def _claim(self, item: _WorkItem) -> bool:
        """Validate a dequeued item against current state. Returns emit/skip."""
        with self._lock:
            if self._stop.is_set():
                return False
            # Stale generation (a seek happened after this was scheduled): drop.
            if item.generation != self.generation:
                self.dropped_count += 1
                return False
            if self.state is not PlayState.PLAYING:
                # Paused/stopped after dequeue but before emit: make sure this
                # position is rescheduled on resume, then skip emitting now.
                if item.pos < self.scheduled_pos:
                    self.scheduled_pos = item.pos
                return False
            # Emitting exactly at the expected emitted position guarantees
            # in-order, once-only delivery within a generation.
            if item.pos != self.emitted_pos:
                # A later duplicate from a flushed window (shouldn't normally
                # happen, but keep the invariant strict): reschedule.
                if item.pos < self.scheduled_pos:
                    self.scheduled_pos = item.pos
                return False
            self.emitted_pos += 1
            return True

    def _emit(self, item: _WorkItem) -> None:
        entry = item.entry
        msg_type = self._index.topics.get(entry.topic)
        record = PublishedRecord(
            generation=item.generation,
            seq=entry.seq,
            topic=entry.topic,
            msg_type=msg_type.type if msg_type else "",
            bag_timestamp_ns=entry.timestamp_ns,
            wall_published_ns=_now_ns(),
            data_len=len(entry.data),
        )
        self._sink.emit(record, entry.data)
        with self._lock:
            self.published_count += 1
            if (
                self.emitted_pos >= len(self._all)
                and self.scheduled_pos >= len(self._all)
                and not self._heap
            ):
                self.state = PlayState.FINISHED
                self.finished_ns = _now_ns()
