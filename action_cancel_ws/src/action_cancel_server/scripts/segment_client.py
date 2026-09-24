#!/usr/bin/env python3
"""Command-line client for the segmented synthesis action server.

Subcommands
-----------
  send   send one goal; optionally cancel after a given segment completes,
         and optionally discard every feedback message (simulates a client
         that missed feedback over the wire — terminal truth must still
         match durable history).
  get    fetch one goal's durable record (including per-segment rows).
  list   list durable goal records, optionally filtered by state.

All output is JSON to stdout; the process exits non-zero on rejected goals
or protocol errors so the acceptance script can assert on it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

import rclpy
from rclpy.action import ActionClient
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node

from action_cancel_interfaces.action import SegmentedTask
from action_cancel_interfaces.srv import GetGoal, ListGoals
from action_cancel_server import states

STATE_NAME = states.STATE_NAMES
POLICY_RESUME = states.POLICY_RESUME
POLICY_ABORT = states.POLICY_ABORT


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)


class TaskClient(Node):
    def __init__(self, action_name: str):
        base = action_name
        super().__init__("segment_task_client")
        self._action_name = base
        # Keep a STRONG reference: Node.executor only stores a weakref, so
        # assigning self.executor alone lets the executor be GC'd instantly.
        self._exec = SingleThreadedExecutor(context=self.context)
        self._exec.add_node(self)
        self._ac = ActionClient(self, SegmentedTask, base)
        self._get = self.create_client(GetGoal, base + "/get_goal")
        self._list = self.create_client(ListGoals, base + "/list_goals")

    def destroy_node(self) -> bool:
        try:
            self._exec.shutdown()
        finally:
            return super().destroy_node()

    def spin_once(self, timeout: float) -> None:
        self._exec.spin_once(timeout_sec=timeout)

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ac.wait_for_server(timeout_sec=timeout)

    # -------------------------------------------------------------- send/cancel
    def send(
        self,
        goal_id: str,
        inputs: list[str],
        iterations: int,
        policy: int,
        cancel_after: Optional[int],
        ignore_feedback: bool,
        cancel_delay_s: float,
    ) -> dict:
        goal = SegmentedTask.Goal(
            goal_id=goal_id,
            segment_inputs=inputs,
            pbkdf2_iterations=iterations,
            recovery_policy=policy,
        )
        feedbacks: list[dict] = []

        def on_feedback(fb_msg):
            if ignore_feedback:
                return
            fb = fb_msg.feedback
            feedbacks.append(
                {
                    "segment_index": fb.segment_index,
                    "completed_segments": fb.completed_segments,
                    "segment_hash": fb.segment_hash,
                    "chained_hash": fb.chained_hash,
                }
            )

        send_future = self._ac.send_goal_async(
            goal, feedback_callback=on_feedback
        )
        if not _spin_until(self, send_future, 10.0):
            raise RuntimeError("timed out waiting for goal acceptance")
        gh = send_future.result()
        if not gh.accepted:
            return {
                "accepted": False,
                "goal_id": goal_id,
                "hint": "rejected by server (duplicate/changed params/invalid); "
                "use 'get' to inspect history",
            }

        cancel_sent_at: Optional[float] = None
        cancel_future = None
        if cancel_after is not None:
            # Wait until that many feedbacks arrived (unless we are
            # deliberately ignoring them, in which case use a timed wait),
            # then issue the cancel.
            if ignore_feedback:
                end = time.time() + 30.0
                # rough synchronization: cancel after delay regardless
                if not _spin_until(
                    self,
                    _sleep_future(self, max(cancel_delay_s, 0.001)),
                    max(cancel_delay_s, 0.001) + 5.0,
                ):
                    raise RuntimeError("cancel timer failed")
            else:
                end = time.time() + 30.0
                while len(feedbacks) <= cancel_after and time.time() < end:
                    _spin_once(self, 0.05)
                if len(feedbacks) <= cancel_after:
                    raise RuntimeError(
                        f"only {len(feedbacks)} feedbacks before cancel timeout"
                    )
            cancel_future = gh.cancel_goal_async()
            cancel_sent_at = time.time()

        result_future = gh.get_result_async()
        if not _spin_until(self, result_future, 60.0):
            raise RuntimeError("timed out waiting for result")
        wrapped = result_future.result()
        res = wrapped.result
        code = wrapped.status  # action_msgs/GoalStatus (4=ABORTED, etc.)

        cancel_resp = None
        canceling = 0
        if cancel_future is not None:
            _spin_until(self, cancel_future, 5.0)
            if cancel_future.done():
                r = cancel_future.result()
                # action_msgs/CancelGoal: ERROR_NONE=0 means admitted.
                cancel_resp = r.return_code
                canceling = len(r.goals_canceling)

        return {
            "accepted": True,
            "goal_id": goal_id,
            "action_status": code,
            "terminal_state": res.terminal_state,
            "terminal_state_name": STATE_NAME.get(
                res.terminal_state, str(res.terminal_state)
            ),
            "completed_segments": res.completed_segments,
            "total_segments": res.total_segments,
            "result_hash": res.result_hash,
            "error_code": res.error_code,
            "feedback_received": feedbacks,
            "feedback_count": len(feedbacks),
            "cancel_return_code": cancel_resp,
            "cancel_goals_canceling": canceling,
            "cancel_admitted": cancel_resp == 0 and canceling >= 1,
        }

    # ------------------------------------------------------------- history API
    def get_goal(self, goal_id: str) -> dict:
        if not self._get.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("get_goal service unavailable")
        fut = self._get.call_async(GetGoal.Request(goal_id=goal_id))
        if not _spin_until(self, fut, 10.0):
            raise RuntimeError("get_goal timed out")
        r = fut.result()
        out = {
            "exists": bool(r.exists),
            "goal_id": r.goal_id,
        }
        if r.exists:
            out.update(
                {
                    "state": r.state,
                    "state_name": STATE_NAME.get(r.state, str(r.state)),
                    "completed_segments": r.completed_segments,
                    "total_segments": r.total_segments,
                    "fingerprint": r.fingerprint,
                    "recovery_policy": r.recovery_policy,
                    "result_hash": r.result_hash,
                    "error_code": r.error_code,
                    "segments": [
                        {
                            "segment_index": s.segment_index,
                            "segment_hash": s.segment_hash,
                            "chained_hash": s.chained_hash,
                            "input_fingerprint": s.input_fingerprint,
                            "duration_ms": s.duration_ms,
                            "recovered": bool(s.recovered),
                        }
                        for s in r.segments
                    ],
                }
            )
        return out

    def list_goals(self, state_filter: int) -> dict:
        if not self._list.wait_for_service(timeout_sec=5.0):
            raise RuntimeError("list_goals service unavailable")
        fut = self._list.call_async(ListGoals.Request(state_filter=state_filter))
        if not _spin_until(self, fut, 10.0):
            raise RuntimeError("list_goals timed out")
        r = fut.result()
        return {
            "goals": [
                {
                    "goal_id": g.goal_id,
                    "state": g.state,
                    "state_name": STATE_NAME.get(g.state, str(g.state)),
                    "completed_segments": g.completed_segments,
                    "total_segments": g.total_segments,
                    "recovery_policy": g.recovery_policy,
                    "fingerprint": g.fingerprint,
                    "result_hash": g.result_hash,
                    "error_code": g.error_code,
                }
                for g in r.goals
            ]
        }


# --------------------------------------------------------------------- helpers
def _spin_once(node: Node, timeout: float) -> None:
    node.spin_once(timeout)


def _spin_until(node: Node, future, timeout: float) -> bool:
    end = time.time() + timeout
    while not future.done() and time.time() < end:
        _spin_once(node, 0.05)
    return future.done()


def _sleep_future(node: Node, seconds: float):
    """A future completed by a one-shot timer (used in feedback-ignore mode)."""
    fut = Future()
    timer = node.create_timer(seconds, lambda: (fut.set_result(None),))

    def _done(_):
        try:
            node.destroy_timer(timer)
        except Exception:
            pass

    fut.add_done_callback(_done)
    return fut


def _load_inputs(path: Optional[str], inline: list[str]) -> list[str]:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "segment_inputs" in data:
            data = data["segment_inputs"]
        if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
            raise ValueError("input file must be a JSON list of strings "
                             "(or an object with 'segment_inputs')")
        return data
    return inline or ["alpha", "beta", "gamma"]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--action-name", default="segmented_task")
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("send", help="send a goal")
    ps.add_argument("goal_id")
    ps.add_argument("--inputs-file", help="JSON file: list of strings or "
                     "{'segment_inputs': [...]}")
    ps.add_argument("--input", action="append", default=[],
                    help="one segment input (repeatable)")
    ps.add_argument("--iterations", type=int, default=50_000)
    pol = ps.add_mutually_exclusive_group()
    pol.add_argument("--resume", action="store_const", const=POLICY_RESUME,
                     dest="policy")
    pol.add_argument("--abort-on-recovery", action="store_const",
                     const=POLICY_ABORT, dest="policy")
    ps.set_defaults(policy=POLICY_RESUME)
    ps.add_argument("--cancel-after-index", type=int, default=None,
                    help="cancel once feedback for this 0-based index arrived "
                         "(e.g. last index -> cancel-during-last-segment test)")
    ps.add_argument("--cancel-delay", type=float, default=0.0)
    ps.add_argument("--ignore-feedback", action="store_true",
                    help="discard all feedback messages (feedback-loss test)")

    pg = sub.add_parser("get", help="query durable record of one goal")
    pg.add_argument("goal_id")

    pl = sub.add_parser("list", help="list goal history")
    pl.add_argument("--state", type=int, default=255,
                    help="STATE_* value filter; 255 = all (default)")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rclpy.init(args=argv)
    node = TaskClient(args.action_name)
    try:
        if not node.wait_ready(10.0):
            print(_j({"error": "action server not reachable"}), file=sys.stderr)
            return 3
        if args.cmd == "send":
            inputs = _load_inputs(args.inputs_file, args.input)
            out = node.send(
                goal_id=args.goal_id,
                inputs=inputs,
                iterations=args.iterations,
                policy=args.policy,
                cancel_after=args.cancel_after_index,
                ignore_feedback=args.ignore_feedback,
                cancel_delay_s=args.cancel_delay,
            )
            print(_j(out))
            return 0 if out.get("accepted") else 2
        if args.cmd == "get":
            print(_j(node.get_goal(args.goal_id)))
            return 0
        if args.cmd == "list":
            print(_j(node.list_goals(args.state)))
            return 0
    finally:
        node.destroy_node()
        rclpy.shutdown()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
