#!/usr/bin/env python3
"""Query goal history through the QueryGoal/ListGoals services.

Examples:
    ./query_history.py --list --include-finished
    ./query_history.py --goal-id dup-goal-001
"""

from __future__ import annotations

import argparse
import sys

import rclpy

from segtask_server.clients.client_lib import LIST_SERVICE, query_goal
from segtask_server.status import STATUS_NAMES
from segtask_msgs.srv import ListGoals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--goal-id', help='query a single goal (QueryGoal)')
    parser.add_argument('--list', action='store_true',
                        help='list goals (ListGoals)')
    parser.add_argument('--include-finished', action='store_true')
    parser.add_argument('--limit', type=int, default=50)
    args = parser.parse_args()

    rclpy.init()
    node = rclpy.create_node('query_history')
    try:
        if args.goal_id:
            info = query_goal(node, args.goal_id)
            if info is None:
                print('service unavailable')
                return 2
            if not info.found:
                print(f'{args.goal_id}: not found')
                return 1
            print(f'goal_id              : {info.goal_id}')
            print(f'status               : {STATUS_NAMES.get(info.status, info.status)}')
            print(f'segments             : {info.segments_done}/{info.segments_total}')
            print(f'policy               : {info.policy}')
            print(f'resumed_after_restart: {info.resumed_after_restart}')
            print(f'result_hash          : {info.result_hash}')
            print(f'message              : {info.result_message}')
            print(f'params               : {info.params_json}')
            print(f'chain_ok             : {info.chain_ok} '
                  f'{info.chain_error or ""}'.rstrip())
            return 0 if info.chain_ok else 3

        client = node.create_client(ListGoals, LIST_SERVICE)
        if not client.wait_for_service(timeout_sec=10):
            print('service unavailable')
            return 2
        req = ListGoals.Request()
        req.include_finished = bool(args.include_finished)
        req.limit = int(args.limit)
        future = client.call_async(req)
        deadline_nb = __import__('time').monotonic() + 10
        while not future.done() and __import__('time').monotonic() < deadline_nb:
            rclpy.spin_once(node, timeout_sec=0.1)
        resp = future.result()
        print(f'{resp.message}')
        for g in resp.goals:
            print(f'  {g.goal_id:28s} {STATUS_NAMES.get(g.status, g.status):11s} '
                  f'{g.segments_done}/{g.segments_total}  resumed={int(g.resumed_after_restart)}  '
                  f'hash={g.result_hash[:12]}')
        return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    sys.exit(main())
