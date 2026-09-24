#!/usr/bin/env python3
"""Scenario 4: feedback messages are lost, outcome must not change.

The server is started with feedback_drop_rate=0.6 (60% of feedback publishes
intentionally skipped) and a fixed random seed. We verify:

* the goal still reaches exactly one terminal state: SUCCEEDED;
* segments_done == segments_total and QueryGoal agrees with the action result
  (feedback is advisory; SQLite is the source of truth);
* the number of feedbacks actually received is strictly below the number
  published-and-counted as attempted... concretely: fewer feedbacks arrived
  than segment start events, while result.feedback_dropped > 0;
* canceling a separate goal despite heavy loss still works (loss on one
  channel does not block cancellation).
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time

import rclpy

from segtask_server.clients.client_lib import query_goal, send_goal


def _env(domain: int) -> dict:
    env = dict(os.environ)
    env['ROS_DOMAIN_ID'] = str(domain)
    env['ROS_LOCALHOST_ONLY'] = '1'
    return env


def main() -> int:
    parser = argparse.ArgumentParser()
    default_bin = os.path.expanduser(
        '~/Downloads/biaozhul/P044/a/install/segtask_server/lib/'
        'segtask_server/segtask_server_node')
    parser.add_argument('--node-bin', default=default_bin)
    parser.add_argument('--drop-rate', type=float, default=0.6)
    args = parser.parse_args()

    import tempfile
    tmp = tempfile.mkdtemp(prefix='segtask-loss-')
    db_path = os.path.join(tmp, 'segtask.db')
    domain = 77
    log_path = os.path.join(tmp, 'server.log')
    failures = []

    def check(label, cond, detail=''):
        mark = 'PASS' if cond else 'FAIL'
        print(f'[{mark}] {label}' + (f' -- {detail}' if detail else ''))
        if not cond:
            failures.append(label)

    cmd = [args.node_bin, '--ros-args', '-p', f'db_path:={db_path}',
           '-p', 'recovery_mode:=resume',
           '-p', f'feedback_drop_rate:={args.drop_rate}',
           '-p', 'feedback_drop_seed:=42']
    log = open(log_path, 'w')
    proc = subprocess.Popen(cmd, env=_env(domain), stdout=log,
                            stderr=subprocess.STDOUT)
    os.environ['ROS_DOMAIN_ID'] = str(domain)
    os.environ['ROS_LOCALHOST_ONLY'] = '1'
    rclpy.init()
    node = rclpy.create_node('test_feedback_loss')
    try:
        time.sleep(2.5)
        # Goal A: tolerate heavy feedback loss, must still fully succeed.
        out = send_goal(node, 'loss-goal-a', segments_total=8,
                        work_units=80_000, checkpoint_ms=10, timeout=60)
        check('goal accepted', out.accepted)
        check('goal SUCCEEDED despite lost feedback',
              out.accepted and out.status == 4,
              f'status={out.terminal_name}')
        check('all segments complete', out.segments_done == 8,
              f'done={out.segments_done}/8')
        check('server reports dropped feedback > 0',
              out.feedback_dropped_reported > 0,
              f'dropped={out.feedback_dropped_reported}')
        check('client received fewer feedbacks than events generated',
              len(out.feedbacks) < 16,
              f'received={len(out.feedbacks)} (at most 16 expected attempts)')
        print(f'    feedbacks received={len(out.feedbacks)}/16 attempts, '
              f'server-dropped={out.feedback_dropped_reported}')

        info = query_goal(node, 'loss-goal-a')
        check('persisted state matches action result',
              info is not None and info.status == 4
              and info.segments_done == 8 and info.chain_ok,
              f'status={getattr(info, "status", None)} '
              f'done={getattr(info, "segments_done", None)}')

        # Goal B: cancellation must still win despite feedback loss. We cancel
        # after just ONE arrived feedback.
        out_b = send_goal(node, 'loss-goal-b-cancel', segments_total=10,
                          work_units=300_000, checkpoint_ms=10,
                          cancel_at_segment=0, timeout=60)
        check('second goal CANCELED reliably despite lossy channel',
              out_b.status == 5,
              f'status={out_b.terminal_name} done={out_b.segments_done}/10')
        check('canceled goal has single terminal CANCELED in DB',
              (info_b := query_goal(node, 'loss-goal-b-cancel')) is not None
              and info_b.status == 5 and info_b.segments_done <= 10,
              f'status={getattr(info_b, "status", None)}')
    finally:
        node.destroy_node()
        rclpy.shutdown()
        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)
        log.close()

    if failures:
        print(f'\n{len(failures)} FAIL: {failures}')
        print(f'server log: {log_path}')
        return 1
    print('\nALL CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
