"""Goal execution logic shared by the action callback and crash recovery.

The executor is deliberately dumb about policy: the database decides
everything. Before every segment and at every commit the row is re-read, so a
cancel that landed while the CPU loop was running is observed immediately at
the next checkpoint / commit attempt.

Feedback is explicitly *advisory*: every publish can be dropped
(``feedback_drop_rate``) and the executor never blocks on or retries it. The
SQLite state plus the action result are the source of truth; losing feedback
must not change the outcome.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional

from . import status as st
from .crypto import compute_segment
from .db import Store

GENESIS_SEED = '00' * 32

logger = logging.getLogger('segtask.runner')


class GoalExecutor:
    def __init__(self, store: Store,
                 publish_feedback: Optional[Callable[[str, Any], None]] = None,
                 feedback_drop_rate: float = 0.0,
                 drop_rng: Optional[random.Random] = None):
        self.store = store
        self._publish_feedback = publish_feedback
        self._drop_rate = feedback_drop_rate
        self._rng = drop_rng or random.Random()
        self.dropped_feedback: dict[str, int] = {}
        self.published_feedback: dict[str, int] = {}

    def _feedback(self, goal_id: str, msg: Any) -> None:
        if self._publish_feedback is None:
            return
        if self._rng.random() < self._drop_rate:
            self.dropped_feedback[goal_id] = self.dropped_feedback.get(goal_id, 0) + 1
            logger.warning('[%s] DROPPING feedback (simulated loss): seg %s',
                           goal_id, getattr(msg, 'segment_index', '?'))
            return
        self.published_feedback[goal_id] = self.published_feedback.get(goal_id, 0) + 1
        self._publish_feedback(goal_id, msg)

    def _cancel_seen(self, goal_id: str) -> bool:
        row = self.store.get_goal(goal_id)
        return row is None or bool(row['cancel_requested']) \
            or row['status'] in (st.CANCELING, st.CANCELED)

    def _seed_for_done(self, goal_id: str, done: int) -> str:
        """Chained seed: genesis for segment 0, else last committed hash.

        Reading it from the committed segments table (instead of carrying it in
        memory) means a resumed process reconstructs identical inputs.
        """
        if done == 0:
            return GENESIS_SEED
        segments = self.store.get_segments(goal_id)
        if len(segments) < done:
            raise RuntimeError(
                f'cannot resume {goal_id}: expected {done} committed segments, '
                f'found {len(segments)}')
        return segments[done - 1]['output_hash']

    def run(self, goal_id: str, feedback_factory: Callable[[], Any]
            ) -> Any:
        """Execute until terminal. Returns a feedback/result-shaped message."""
        fb = feedback_factory()
        row = self.store.mark_running(goal_id)
        if row is None:
            # Already terminal (e.g. admin-aborted before recovery started).
            row = self.store.get_goal(goal_id)
        if row is not None and row['status'] in st.TERMINAL_STATUSES:
            return self._final_result(goal_id, row, replay=True)

        while True:
            row = self.store.get_goal(goal_id)
            if row is None:
                raise RuntimeError(f'goal {goal_id} vanished from store')
            if row['status'] in st.TERMINAL_STATUSES:
                return self._final_result(goal_id, row)
            if row['cancel_requested'] or row['status'] == st.CANCELING:
                break

            done = row['segments_done']
            total = row['segments_total']
            if done >= total:
                break

            index = done
            seed = self._seed_for_done(goal_id, done)

            fb.goal_id = goal_id
            fb.segment_index = index
            fb.segments_done = done
            fb.segments_total = total
            fb.status = st.RUNNING
            fb.last_segment_hash = seed
            self._feedback(goal_id, fb)

            checkpoint_period = max(1.0, float(row['checkpoint_ms'])) / 1000.0
            last_check = [time.monotonic()]

            def _poll_cancel() -> bool:
                now = time.monotonic()
                if now - last_check[0] >= checkpoint_period:
                    last_check[0] = now
                    return self._cancel_seen(goal_id)
                return False

            output, canceled = compute_segment(
                seed, int(row['work_units']),
                should_cancel=_poll_cancel, checkpoint_every=50_000)
            if canceled:
                logger.info('[%s] cancellation observed during segment %d',
                            goal_id, index)
                break

            # Commit attempt is the second race point: if the status flipped to
            # CANCELING while we computed, the DB rejects the commit.
            ok, message, updated = self.store.commit_segment(
                goal_id, index, output)
            if not ok:
                logger.info('[%s] %s', goal_id, message)
                break

            fb.goal_id = goal_id
            fb.segment_index = index
            fb.segments_done = updated['segments_done']
            fb.segments_total = total
            fb.status = st.RUNNING
            fb.last_segment_hash = output
            self._feedback(goal_id, fb)

            # Final arbitration happens in the DB even when we appear to have
            # finished every segment; a cancel in the same window may still win.
            if updated['segments_done'] >= total:
                break

        final = self.store.finalize(goal_id)
        if final is None:
            final = self.store.get_goal(goal_id)
        return self._final_result(goal_id, final)

    def _final_result(self, goal_id: str, row, replay: bool = False) -> Any:
        """Return a dict describing the durable terminal outcome."""
        status = row['status'] if row is not None else st.ABORTED
        if status == st.SUCCEEDED:
            code = st.CODE_REPLAYED if replay else st.CODE_SUCCEEDED
        elif status == st.CANCELED:
            code = st.CODE_CANCELED
        elif status == st.ABORTED:
            code = st.CODE_ABORTED
        else:
            code = st.CODE_FAILED
        return {
            'code': code,
            'success': status == st.SUCCEEDED,
            'goal_id': goal_id,
            'status': status,
            'segments_done': row['segments_done'] if row else 0,
            'segments_total': row['segments_total'] if row else 0,
            'result_hash': row['result_hash'] if row else '',
            'message': row['result_message'] if row else 'goal missing',
        }
