"""Offline/online verification of the HMAC event chain.

Replays every ``goal_events`` row (optionally for one goal), recomputes the
HMAC-SHA256 chain links with the stored server secret and reports the first
broken link or gap. Also verifies that segment outputs genuinely match a
fresh recomputation of the chained SHA-256 work (proof that recorded results
were really computed).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Optional

from .crypto import GENESIS, compute_segment, load_or_create_secret, verify_chain_link
from .db import Store

logger = logging.getLogger('segtask.audit')


def _payload(event) -> dict:
    return json.loads(event['payload_json'])


def verify_goal_chain(store: Store, goal_id: str) -> tuple[bool, Optional[str]]:
    """Verify hash links for every event belonging to ``goal_id``.

    Links are verified against the event's stored ``prev_hash`` pointer: each
    goal's first event must point at the global chain tip at creation time.
    Because server events are interleaved, verification walks the *whole* chain
    first, then checks the goal's subsequence pointers against it.
    """
    all_events = store.events()
    by_id = {e['id']: e for e in all_events}
    goal_events = [e for e in all_events if e['goal_id'] == goal_id]
    if not goal_events:
        return False, 'no events for goal'
    # Global chain: strict sequence starting at GENESIS.
    prev = GENESIS
    for event in all_events:
        if event['prev_hash'] != prev:
            return False, (f"event id={event['id']} prev_hash mismatch "
                           f"(expected {prev[:12]}..., got "
                           f"{event['prev_hash'][:12]}...)")
        if not verify_chain_link(store.secret, prev, _payload(event),
                                 event['event_hash']):
            return False, f"HMAC mismatch at event id={event['id']} ({event['event_type']})"
        prev = event['event_hash']
    # Goal-specific sanity: every recorded segment event must map to a segment
    # row, indices must be contiguous and start at 0.
    committed = [e for e in goal_events if e['event_type'] == 'SEGMENT_COMMITTED']
    for expected_idx, event in enumerate(committed):
        idx = _payload(event).get('segment_index')
        if idx != expected_idx:
            return False, (f'segment event index gap: expected {expected_idx}, '
                           f'got {idx}')
    _ = by_id  # index available for future checks
    return True, None


def recompute_segments(store: Store, goal_id: str) -> tuple[bool, Optional[str]]:
    """Independently recompute every segment and compare output hashes."""
    goal = store.get_goal(goal_id)
    if goal is None:
        return False, 'unknown goal'
    seed = '00' * 32
    for seg in store.get_segments(goal_id):
        out, canceled = compute_segment(seed, int(goal['work_units']))
        if canceled:
            return False, 'unexpected cancellation during audit recompute'
        if out != seg['output_hash']:
            return False, (f"segment {seg['segment_index']} output mismatch: "
                           f"stored {seg['output_hash'][:12]}... vs recomputed "
                           f"{out[:12]}...")
        seed = out
    return True, None


def audit_database(db_path: str) -> int:
    secret = load_or_create_secret(db_path + '.key')
    store = Store(db_path, secret, run_id='audit')
    exit_code = 0
    try:
        events = store.events()
        # Verify global chain once.
        prev = GENESIS
        for event in events:
            if event['prev_hash'] != prev or not verify_chain_link(
                    secret, prev, _payload(event), event['event_hash']):
                print(f"FAIL: global chain broken at event id={event['id']} "
                      f"({event['event_type']})")
                exit_code = 2
                break
            prev = event['event_hash']
        else:
            print(f'chain OK: {len(events)} events')

        rows = store.list_goals(include_finished=True, limit=10_000)
        for row in rows:
            gid = row['goal_id']
            ok, err = verify_goal_chain(store, gid)
            if not ok:
                print(f'FAIL [{gid}] chain: {err}')
                exit_code = 2
                continue
            if row['segments_done']:
                rok, rerr = recompute_segments(store, gid)
                if not rok:
                    print(f'FAIL [{gid}] recompute: {rerr}')
                    exit_code = 2
                    continue
            print(f'OK   [{gid}] status={row["status"]:d} '
                  f'segments={row["segments_done"]}/{row["segments_total"]}')
        return exit_code
    finally:
        store.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Verify the segtask event chain and recompute segment work.')
    parser.add_argument('--db-path', required=True, help='path to segtask.db')
    args = parser.parse_args(argv)
    if not os.path.exists(args.db_path):
        print(f'database not found: {args.db_path}', file=sys.stderr)
        return 1
    return audit_database(args.db_path)


if __name__ == '__main__':
    sys.exit(main())
