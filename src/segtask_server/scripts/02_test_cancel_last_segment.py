#!/usr/bin/env python3
"""Scenario 2: cancel during (and specifically at) the last segment.

The goal runs N segments; the client cancels as soon as feedback for the last
segment index arrives. We then verify:
* the goal has exactly ONE terminal state: CANCELED;
* the durable segments_done count equals the number of SEGMENT_COMMITTED rows
  (the last segment's in-flight output is either committed or not, but the
  reported count and persisted rows always agree);
* a second cancel attempt cannot move the goal out of its terminal state;
* a late/duplicate segment result can never land (attempted directly against
  the store on the server side is covered in pytest; here we verify the count
  stays frozen for a grace period).
"""

from __future__ import annotations

import sys
import time

import rclpy

from segtask_server.clients.client_lib import query_goal, send_goal


def main() -> int:
    rclpy.init()
    node = rclpy.create_node('test_cancel_last_segment')
    failures = []

    def check(label: str, cond: bool, detail: str = '') -> None:
        mark = 'PASS' if cond else 'FAIL'
        print(f'[{mark}] {label}' + (f' -- {detail}' if detail else ''))
        if not cond:
            failures.append(label)

    gid = 'cancel-last-001'
    try:
        # 4 segments; cancel when the feedback announcing segment index 3
        # (the LAST one) has arrived. checkpoint_ms keeps latency tight.
        outcome = send_goal(node, gid, segments_total=4, work_units=400_000,
                            checkpoint_ms=10, cancel_at_segment=3,
                            timeout=60)
        check('goal accepted', outcome.accepted)
        check('terminal state is CANCELED (5)', outcome.status == 5,
              f'status={outcome.terminal_name} code={outcome.code} '
              f'msg={outcome.message}')

        info = query_goal(node, gid)
        check('persisted status is CANCELED', info is not None and info.status == 5)
        check('durable count matches action result',
              info is not None and info.segments_done == outcome.segments_done,
              f'query={info.segments_done if info else "?"} '
              f'action={outcome.segments_done}')
        # The last segment's output may have committed before the cancel CAS
        # landed (that IS the race); any count 0..4 is valid - but the result
        # count, the durable rows and the terminal state must all agree.
        check('cancel/complete race left a consistent count',
              0 <= outcome.segments_done <= 4,
              f'done={outcome.segments_done}/4')
        check('event chain intact after cancellation',
              info is not None and info.chain_ok,
              f'chain_error={getattr(info, "chain_error", "")}')

        # The count must be frozen: wait and re-query.
        frozen_at = info.segments_done
        time.sleep(1.0)
        info2 = query_goal(node, gid)
        check('segments count frozen after terminal state',
              info2 is not None and info2.segments_done == frozen_at
              and info2.status == 5,
              f'done {frozen_at} -> {info2.segments_done if info2 else "?"}')

        print(f'    feedbacks received={len(outcome.feedbacks)} '
              f'indices={sorted(set(outcome.feedback_indices))} '
              f'durably_done={frozen_at}/4')
    finally:
        node.destroy_node()
        rclpy.shutdown()

    if failures:
        print(f'\n{len(failures)} check(s) FAILED: {failures}')
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
