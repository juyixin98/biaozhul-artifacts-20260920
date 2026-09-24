#!/usr/bin/env python3
"""Submit the example goals from examples/goals.json and report each outcome.

Reads JSON entries (goal_id, segments_total, work_units, checkpoint_ms,
policy), sends them to the running action server one by one, and prints the
action outcome plus the durable QueryGoal state. Edit/extend the JSON to try
your own inputs.

Usage:
    ./submit_examples.py [--file PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import rclpy

from segtask_server.clients.client_lib import query_goal, send_goal
from segtask_server.status import STATUS_NAMES


def main() -> int:
    parser = argparse.ArgumentParser()
    try:
        from ament_index_python.packages import get_package_share_directory
        share_dir = get_package_share_directory('segtask_server')
    except Exception:
        share_dir = os.path.join(os.path.dirname(__file__), '..', '..', '..',
                                 'src', 'segtask_server')
    default_file = os.path.join(share_dir, 'examples', 'goals.json')
    parser.add_argument('--file', default=default_file)
    args = parser.parse_args()

    with open(args.file, encoding='utf-8') as fh:
        goals = json.load(fh)

    rclpy.init()
    node = rclpy.create_node('example_submitter')
    failures = 0
    try:
        for spec in goals:
            gid = spec['goal_id']
            print(f"-> submitting {gid}: {spec['segments_total']} segments x "
                  f"{spec['work_units']} units, policy={spec.get('policy', 'resume')}")
            out = send_goal(
                node, gid,
                segments_total=spec['segments_total'],
                work_units=spec['work_units'],
                checkpoint_ms=spec.get('checkpoint_ms', 50),
                policy=spec.get('policy', 'resume'),
                timeout=120)
            if not out.accepted:
                print(f'   REJECTED by server (duplicate active goal or '
                      f'id/parameter conflict): {gid}')
                failures += 1
                continue
            print(f'   action outcome: {out.status_name} '
                  f'{out.segments_done}/{out.segments_total} code={out.code} '
                  f'hash={out.result_hash[:16]}')
            info = query_goal(node, gid)
            if info is None or not info.found:
                print('   QueryGoal: NOT FOUND (unexpected)')
                failures += 1
                continue
            print(f'   durable state: {STATUS_NAMES.get(info.status)} '
                  f'{info.segments_done}/{info.segments_total} '
                  f'resumed={info.resumed_after_restart} chain_ok={info.chain_ok}')
            if info.status not in (4, 5, 6) or not info.chain_ok:
                failures += 1
    finally:
        node.destroy_node()
        rclpy.shutdown()

    print(f'\n{len(goals) - failures}/{len(goals)} goals reported a healthy '
          'terminal state')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
