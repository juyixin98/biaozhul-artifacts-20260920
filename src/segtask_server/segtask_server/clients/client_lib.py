"""Thin blocking client used by the demo/test scripts.

The client records:
* every feedback message actually received (the server may drop some on
  purpose) with per-segment counts;
* the final action outcome;
* the durable state from QueryGoal (the independently persisted truth).
"""

from __future__ import annotations

import dataclasses
import time
from typing import Optional

import rclpy
from rclpy.action import ActionClient

from segtask_msgs.action import SegmentTask
from segtask_msgs.srv import QueryGoal

from ..status import STATUS_NAMES

# Absolute names of the server's interfaces. The server declares them as
# private ('~/...') on node 'segtask_server', which expands to these paths;
# clients must use the absolute form because '~' expands to the CLIENT node.
ACTION_NAME = '/segtask_server/segment_task'
QUERY_SERVICE = '/segtask_server/query_goal'
LIST_SERVICE = '/segtask_server/list_goals'
ADMIN_SERVICE = '/segtask_server/admin'


@dataclasses.dataclass
class GoalOutcome:
    goal_id: str
    accepted: bool
    status: int = -1
    status_name: str = 'UNKNOWN'
    segments_done: int = -1
    segments_total: int = -1
    result_hash: str = ''
    code: int = -1
    message: str = ''
    feedback_dropped_reported: int = 0
    feedbacks: list = dataclasses.field(default_factory=list)
    feedback_indices: list = dataclasses.field(default_factory=list)

    @property
    def terminal_name(self) -> str:
        return STATUS_NAMES.get(self.status, f'UNKNOWN({self.status})')


def wait_for_action_server(client: ActionClient, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.wait_for_server(timeout_sec=0.5):
            return True
    return False


def send_goal(node, goal_id: str, segments_total: int, work_units: int,
              checkpoint_ms: int = 50, policy: str = 'resume',
              cancel_at_segment: Optional[int] = None,
              timeout: float = 60.0
              ) -> GoalOutcome:
    """Send one goal; optionally cancel once a segment index is seen.

    Cancellation fires as soon as ANY received feedback names a
    ``segment_index >= cancel_at_segment``. The trigger is index-based rather
    than feedback-count based, so it remains correct when the server drops
    intermediate feedback. Returns when the goal is terminal (or rejected).
    """
    action_client = ActionClient(node, SegmentTask, ACTION_NAME)
    if not wait_for_action_server(action_client, timeout):
        return GoalOutcome(goal_id=goal_id, accepted=False)

    goal = SegmentTask.Goal()
    goal.goal_id = goal_id
    goal.segments_total = int(segments_total)
    goal.work_units = int(work_units)
    goal.checkpoint_ms = int(checkpoint_ms)
    goal.policy = policy

    outcome = GoalOutcome(goal_id=goal_id, accepted=False)
    cancel_fired = {'value': False}

    def on_feedback(msg):
        outcome.feedbacks.append(msg)
        outcome.feedback_indices.append(int(msg.feedback.segment_index))

    send_future = action_client.send_goal_async(goal, feedback_callback=on_feedback)
    if not _spin_until(node, send_future, timeout):
        return outcome
    handle = send_future.result()
    outcome.accepted = bool(handle.accepted)
    if not handle.accepted:
        return outcome

    result_future = handle.get_result_async()

    def _maybe_cancel():
        # Fire exactly once, index-based; works even when earlier feedbacks
        # were dropped by the server.
        if cancel_at_segment is None or cancel_fired['value']:
            return
        seen = outcome.feedback_indices
        if seen and max(seen) >= cancel_at_segment:
            cancel_fired['value'] = True
            node.get_logger().info(
                f'[{goal_id}] client requesting cancel (segment '
                f'{max(seen)} >= {cancel_at_segment})')
            handle.cancel_goal_async()

    while not result_future.done():
        _maybe_cancel()
        _spin_once(node, 0.02)
    _maybe_cancel()

    result = result_future.result().result
    outcome.code = int(result.code)
    outcome.status = int(result.status)
    outcome.status_name = STATUS_NAMES.get(outcome.status, 'UNKNOWN')
    outcome.segments_done = int(result.segments_done)
    outcome.segments_total = int(result.segments_total)
    outcome.result_hash = result.result_hash
    outcome.message = result.message
    outcome.feedback_dropped_reported = int(result.feedback_dropped)
    return outcome


def query_goal(node, goal_id: str, timeout: float = 5.0) -> Optional[object]:
    client = node.create_client(QueryGoal, QUERY_SERVICE)
    if not client.wait_for_service(timeout_sec=timeout):
        return None
    req = QueryGoal.Request()
    req.goal_id = goal_id
    future = client.call_async(req)
    if not _spin_until(node, future, timeout):
        return None
    return future.result()


def _spin_until(node, future, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while not future.done() and time.monotonic() < deadline:
        _spin_once(node, 0.05)
    return future.done()


def _spin_once(node, period: float) -> None:
    # rclpy.spin_once works even when the node is not attached to a long-lived
    # executor (it temporarily adds it to a single-shot executor).
    try:
        rclpy.spin_once(node, timeout_sec=period)
    except Exception:
        time.sleep(period)
