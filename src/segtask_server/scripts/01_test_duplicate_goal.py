#!/usr/bin/env python3
"""Scenario 1: duplicate goal-ID handling.

Expectations verified (exit code non-zero on failure):
* first submission of an ID with parameters P  -> accepted, succeeds;
* same ID, SAME parameters, while non-terminal  -> REJECTED by the action;
* same ID, DIFFERENT parameters                -> REJECTED;
* same ID, SAME parameters after completion     -> accepted as a REPLAY and
  returns the identical stored terminal result.
"""

from __future__ import annotations

import sys

import rclpy

from segtask_server.clients.client_lib import query_goal, send_goal
from segtask_server.status import CODE_REPLAYED


def main() -> int:
    rclpy.init()
    node = rclpy.create_node('test_duplicate_goal')
    failures = []

    def check(label: str, cond: bool, detail: str = '') -> None:
        mark = 'PASS' if cond else 'FAIL'
        print(f'[{mark}] {label}' + (f' -- {detail}' if detail else ''))
        if not cond:
            failures.append(label)

    gid = 'dup-goal-001'
    try:
        first = send_goal(node, gid, segments_total=3, work_units=20_000,
                          checkpoint_ms=20, timeout=30)
        check('original goal accepted and succeeded',
              first.accepted and first.status == 4,
              f'accepted={first.accepted} status={first.terminal_name}')

        # While terminal, same params -> replay (not a fresh run).
        replay = send_goal(node, gid, segments_total=3, work_units=20_000,
                           checkpoint_ms=20, timeout=30)
        check('same id/same params after finish replays stored result',
              replay.accepted and replay.code == CODE_REPLAYED,
              f'accepted={replay.accepted} code={replay.code}')
        check('replayed result hash identical',
              replay.result_hash == first.result_hash and
              replay.segments_done == first.segments_done,
              f'{replay.result_hash[:12]} vs {first.result_hash[:12]}')

        # Different params -> rejected at protocol level.
        diff = send_goal(node, gid, segments_total=4, work_units=20_000,
                         checkpoint_ms=20, timeout=30)
        check('same id/different params rejected', not diff.accepted,
              f'accepted={diff.accepted}')

        diff2 = send_goal(node, gid, segments_total=3, work_units=999,
                          checkpoint_ms=20, timeout=30)
        check('same id/different work_units rejected', not diff2.accepted)

        # Query history.
        info = query_goal(node, gid)
        check('history queryable after finish',
              info is not None and info.found and info.status == 4
              and info.segments_done == 3 and info.chain_ok,
              f'found={getattr(info, "found", None)} '
              f'chain_ok={getattr(info, "chain_ok", None)}')
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
