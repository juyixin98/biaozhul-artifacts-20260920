#!/usr/bin/env python3
"""Scenario 3: hard process interruption mid-goal, then restart.

This script manages the server process itself:

1. starts ``segtask_server_node`` (resume mode) on an isolated ROS_DOMAIN_ID;
2. submits a long goal (fire-and-forget), waits until SQLite shows committed
   segments, then SIGKILLs the server mid-segment;
3. checks the on-disk state directly with sqlite3 (goal was RUNNING,
   segments_done between 1 and N-1, segment rows match the count);
4. restarts the server and proves the goal is marked RECOVERING then resumed:
   it reaches SUCCEEDED, segments_done == total and every committed segment
   hash independently recomputes via the offline audit tool (real work, not
   fabricated);
5. reruns the whole flow with a goal whose own policy is ``abort`` and proves
   it ends ABORTED with its partial work preserved.

Usage:
    ./03_test_process_crash.py [--node-bin PATH] [--work-units N]
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

import rclpy
from rclpy.action import ActionClient

from segtask_msgs.action import SegmentTask
from segtask_server.clients.client_lib import (ACTION_NAME, _spin_once,
                                               query_goal,
                                               wait_for_action_server)
from segtask_server.status import STATUS_NAMES

STATUS_RUNNING, STATUS_RECOVERING = 1, 3
STATUS_SUCCEEDED, STATUS_ABORTED = 4, 6


def _env(domain: int) -> dict:
    env = dict(os.environ)
    env['ROS_DOMAIN_ID'] = str(domain)
    env['ROS_LOCALHOST_ONLY'] = '1'
    return env


def start_server(node_bin: str, db_path: str, domain: int, mode: str,
                 log_path: str) -> subprocess.Popen:
    cmd = [node_bin, '--ros-args', '-p', f'db_path:={db_path}',
           '-p', f'recovery_mode:={mode}']
    log = open(log_path, 'w')
    return subprocess.Popen(cmd, env=_env(domain), stdout=log,
                            stderr=subprocess.STDOUT)


def read_db(db_path: str, goal_id: str) -> dict:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            'SELECT status, segments_done, segments_total, resumed_after_restart, '
            'cancel_requested, result_hash FROM goals WHERE goal_id=?',
            (goal_id,)).fetchone()
        segs = con.execute(
            'SELECT segment_index, output_hash FROM segments WHERE goal_id=? '
            'ORDER BY segment_index', (goal_id,)).fetchall()
        events = con.execute(
            'SELECT event_type FROM goal_events WHERE goal_id=? ORDER BY id',
            (goal_id,)).fetchall()
    finally:
        con.close()
    if row is None:
        raise RuntimeError(f'goal {goal_id} missing from db')
    return {'status': row[0], 'segments_done': row[1], 'total': row[2],
            'resumed': bool(row[3]), 'cancel_requested': bool(row[4]),
            'result_hash': row[5], 'segments': segs,
            'events': [e[0] for e in events]}


def crash_and_recover(node_bin: str, audit_bin: str, policy: str,
                      work_units: int) -> int:
    failures: list[str] = []

    def check(label, cond, detail=''):
        mark = 'PASS' if cond else 'FAIL'
        print(f'[{mark}] {label}' + (f' -- {detail}' if detail else ''))
        if not cond:
            failures.append(label)

    tmp = tempfile.mkdtemp(prefix='segtask-crash-')
    db_path = os.path.join(tmp, 'segtask.db')
    domain = 70 + (0 if policy == 'resume' else 1)
    gid = f'crash-{policy}-001'
    log1 = os.path.join(tmp, 'server1.log')
    log2 = os.path.join(tmp, 'server2.log')
    proc = None

    os.environ['ROS_DOMAIN_ID'] = str(domain)
    os.environ['ROS_LOCALHOST_ONLY'] = '1'
    rclpy.init()
    node = rclpy.create_node(f'crash_observer_{policy}')
    try:
        print(f'--- goal policy={policy} (workspace {tmp}) ---')
        proc = start_server(node_bin, db_path, domain, 'resume', log1)

        client = ActionClient(node, SegmentTask, ACTION_NAME)
        if not wait_for_action_server(client, 20):
            check('server discoverable', False)
            return 1
        goal = SegmentTask.Goal()
        goal.goal_id = gid
        goal.segments_total = 6
        goal.work_units = work_units
        goal.checkpoint_ms = 10
        goal.policy = policy
        goal_future = client.send_goal_async(goal)  # detach after acceptance
        deadline = time.monotonic() + 15
        while not goal_future.done() and time.monotonic() < deadline:
            _spin_once(node, 0.1)
        check('goal accepted before crash',
              goal_future.done() and goal_future.result().accepted)

        # Wait until partial progress is durable, then SIGKILL.
        deadline = time.monotonic() + 25
        partial = None
        while time.monotonic() < deadline:
            if os.path.exists(db_path):
                try:
                    partial = read_db(db_path, gid)
                except Exception:
                    partial = None
                if partial and 1 <= partial['segments_done'] < 6:
                    break
            time.sleep(0.1)
        check('partial durable progress before crash',
              partial is not None and 1 <= partial['segments_done'] < 6,
              f'done={partial["segments_done"] if partial else "?"} '
              f'status={STATUS_NAMES.get(partial["status"]) if partial else "?"}')
        done_before = partial['segments_done'] if partial else 0

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        proc = None
        print(f'    server SIGKILLed after {done_before}/6 segments')

        after = read_db(db_path, gid)
        check('state on disk was RUNNING at crash',
              after['status'] == STATUS_RUNNING,
              f'status={STATUS_NAMES[after["status"]]}')
        check('committed segment rows match segments_done',
              len(after['segments']) == after['segments_done'] == done_before,
              f'rows={len(after["segments"])} done={after["segments_done"]}')

        # Restart (node-level resume mode; per-goal policy decides the outcome).
        proc2 = start_server(node_bin, db_path, domain, 'resume', log2)
        try:
            observed_recovering = False
            t0 = time.monotonic()
            while time.monotonic() - t0 < 3.0:
                try:
                    cur = read_db(db_path, gid)
                    if cur['status'] == STATUS_RECOVERING:
                        observed_recovering = True
                        break
                    if cur['status'] in (STATUS_SUCCEEDED, STATUS_ABORTED):
                        break
                except sqlite3.OperationalError:
                    pass
                time.sleep(0.05)

            final_info = None
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                final_info = query_goal(node, gid, timeout=2.0)
                if final_info and final_info.found \
                        and final_info.status in (4, 5, 6):
                    break
                time.sleep(0.2)
            check('goal reached a terminal state after restart',
                  final_info is not None,
                  STATUS_NAMES.get(final_info.status) if final_info else 'timeout')
            final_db = read_db(db_path, gid)

            if policy == 'resume':
                check('was marked RECOVERING during restart',
                      observed_recovering or final_db['resumed'],
                      f'observed={observed_recovering} flag={final_db["resumed"]}')
                check('resumed goal SUCCEEDED',
                      final_info is not None
                      and final_info.status == STATUS_SUCCEEDED,
                      STATUS_NAMES.get(final_info.status) if final_info else '?')
                check('all 6 segments durably present after resume',
                      final_db['segments_done'] == 6
                      and len(final_db['segments']) == 6,
                      f'done={final_db["segments_done"]} '
                      f'rows={len(final_db["segments"])}')
                check('resumed_after_restart flag set', final_db['resumed'])
                check('RECOVERY_MARKED event present',
                      'RECOVERY_MARKED' in final_db['events'])
                check('event chain intact', final_info.chain_ok,
                      final_info.chain_error)
            else:
                check('abort-policy goal ended ABORTED',
                      final_info is not None
                      and final_info.status == STATUS_ABORTED,
                      STATUS_NAMES.get(final_info.status) if final_info else '?')
                check('partial work preserved under abort policy',
                      final_db['segments_done'] == done_before,
                      f'{done_before} -> {final_db["segments_done"]}')
                check('RECOVERY_ABORTED event present',
                      'RECOVERY_ABORTED' in final_db['events'],
                      str(final_db['events']))
                check('event chain intact', final_info.chain_ok,
                      final_info.chain_error)
        finally:
            os.kill(proc2.pid, signal.SIGTERM)
            proc2.wait(timeout=15)

        # Offline audit: recomputes every segment hash with real SHA-256.
        audit = subprocess.run([audit_bin, '--db-path', db_path],
                               capture_output=True, text=True)
        check('offline audit (chain + segment recomputation) passes',
              audit.returncode == 0,
              (audit.stdout + audit.stderr).strip()[-400:])
    except Exception as exc:
        check(f'scenario ran without exceptions ({exc!r})', False)
    finally:
        node.destroy_node()
        rclpy.shutdown()
        if proc is not None:
            os.kill(proc.pid, signal.SIGTERM)
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print(f'\n{policy}: {len(failures)} FAIL: {failures}')
        return 1
    print(f'\n{policy}: ALL CHECKS PASSED')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    default_bin = os.path.expanduser(
        '~/Downloads/biaozhul/P044/a/install/segtask_server/lib/'
        'segtask_server/segtask_server_node')
    parser.add_argument('--node-bin', default=default_bin)
    parser.add_argument('--audit-bin', default=default_bin.replace(
        'segtask_server_node', 'segtask_audit'))
    parser.add_argument('--work-units', type=int, default=2_000_000)
    args = parser.parse_args()

    rc1 = crash_and_recover(args.node_bin, args.audit_bin, 'resume',
                            args.work_units)
    rc2 = crash_and_recover(args.node_bin, args.audit_bin, 'abort',
                            args.work_units)
    return rc1 or rc2


if __name__ == '__main__':
    sys.exit(main())
