"""Segmented synthesis action server.

Implements, on top of :mod:`storage` and :mod:`crypto`:

* goal-id deduplication — same id with different parameters is rejected and
  recorded; exact resends of terminal goals return the durable result;
* the cancel/complete race — SQLite write transactions serialize the two and
  only one terminal state is ever produced;
* post-cancel rejection — a segment that finishes after the goal is CANCELED
  is not written and not acknowledged;
* crash recovery — on startup every PENDING/RUNNING goal becomes
  RECOVERABLE and is resumed or aborted per the goal's explicit policy;
* history — ``~get_goal`` / ``~list_goals`` services backed by SQLite.

No real robot is driven; the "work" per segment is real PBKDF2/SHA-256.
"""

from __future__ import annotations

import argparse
import dataclasses
import threading
import time
import uuid
from typing import Optional

import rclpy
import rclpy._rclpy_pybind11 as _rclpy_bindings
from action_msgs.msg import GoalStatus
from rclpy.action import ActionServer
from rclpy.action.server import ServerGoalHandle
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.task import Future

from action_cancel_interfaces.action import SegmentedTask
from action_cancel_interfaces.msg import GoalSummary, SegmentRecord
from action_cancel_interfaces.srv import GetGoal, ListGoals

from . import crypto, states
from .storage import CommitResult, GoalRow, Storage

CHAIN_GENESIS = crypto.CHAIN_GENESIS


def _uuid_bytes(value) -> bytes:
    """Normalize the several shapes a ROS action UUID can take:
    stdlib ``uuid.UUID``, ``unique_identifier_msgs/UUID`` (``.uuid`` field),
    or a uint8 array-like."""
    if isinstance(value, uuid.UUID):
        return value.bytes
    inner = getattr(value, "uuid", None)
    if inner is not None and not isinstance(inner, property):
        return bytes(inner)
    arr = getattr(value, "tolist", None)
    if callable(arr):
        return bytes(arr())
    return bytes(value)


@dataclasses.dataclass
class _Active:
    goal_id: str
    handle: Optional[ServerGoalHandle]   # None for headless post-crash resume
    event: threading.Event
    started_at: float


def _validate_goal(goal) -> str:
    """Return '' when the goal is well-formed, else an error code."""
    if not goal.goal_id or not goal.goal_id.strip():
        return states.ERR_EMPTY_ID
    if len(goal.segment_inputs) == 0:
        return states.ERR_EMPTY_INPUTS
    if goal.pbkdf2_iterations <= 0:
        return states.ERR_BAD_ITERATIONS
    if goal.recovery_policy not in (states.POLICY_RESUME, states.POLICY_ABORT):
        return states.ERR_BAD_POLICY
    return ""


class SegmentTaskServer(Node):
    def __init__(
        self,
        db_path: str,
        action_name: str = "segmented_task",
        segment_delay_s: float = 0.0,
    ):
        super().__init__("segment_task_server")
        self.declare_parameter("db_path", db_path)
        self.declare_parameter("action_name", action_name)
        self.declare_parameter("segment_delay_s", segment_delay_s)

        self._db_path = self.get_parameter("db_path").value
        self._action_name = self.get_parameter("action_name").value
        self._segment_delay = float(self.get_parameter("segment_delay_s").value)

        self._storage = Storage(self._db_path)
        self._active: dict[str, _Active] = {}
        self._active_lock = threading.Lock()

        self._cb_group = ReentrantCallbackGroup()
        self._action_server = ActionServer(
            self,
            SegmentedTask,
            self._action_name,
            execute_callback=self._execute_cb,
            goal_callback=self._goal_cb,
            cancel_callback=self._cancel_cb,
            callback_group=self._cb_group,
        )
        self._get_srv = self.create_service(
            GetGoal, self._action_name + "/get_goal", self._get_goal_cb,
            callback_group=self._cb_group,
        )
        self._list_srv = self.create_service(
            ListGoals, self._action_name + "/list_goals", self._list_goals_cb,
            callback_group=self._cb_group,
        )

        # Startup recovery runs once, after the node starts spinning.
        self._recovery_timer = self.create_timer(
            0.05, self._startup_recovery, callback_group=self._cb_group
        )

        self.get_logger().info(
            f"segment task server ready: action='{self._action_name}' "
            f"db='{self._db_path}' segment_delay={self._segment_delay:.3f}s"
        )

    def destroy_node(self) -> bool:
        try:
            self._action_server.destroy()
        finally:
            self._storage.close()
        return super().destroy_node()

    # ============================================================ goal intake
    def _goal_cb(self, goal_request):
        goal = goal_request
        err = _validate_goal(goal)
        if err:
            self.get_logger().warn(
                f"reject goal (validation): id={goal.goal_id!r} reason={err}"
            )
            return rclpy.action.GoalResponse.REJECT

        fingerprint = crypto.goal_fingerprint(
            goal.goal_id,
            list(goal.segment_inputs),
            int(goal.pbkdf2_iterations),
            int(goal.recovery_policy),
        )
        status, existing = self._storage.create_goal(
            goal.goal_id,
            list(goal.segment_inputs),
            int(goal.pbkdf2_iterations),
            int(goal.recovery_policy),
            fingerprint,
        )
        if status == "created":
            self.get_logger().info(
                f"accept new goal id={goal.goal_id} "
                f"segments={len(goal.segment_inputs)} policy={goal.recovery_policy}"
            )
            return rclpy.action.GoalResponse.ACCEPT

        # Duplicate goal id. Parameter mismatch is always rejected; exact
        # duplicates of active/recoverable goals are rejected too (use the
        # running goal / query history instead of creating a second handle).
        mismatch = existing.fingerprint != fingerprint
        if mismatch:
            reason = states.ERR_CHANGED_PARAMS
        elif status == "duplicate_terminal":
            reason = states.ERR_DUP_FINISHED
        elif status == "duplicate_recoverable":
            reason = states.ERR_DUP_RECOVERABLE
        else:
            reason = states.ERR_DUP_ACTIVE
        self._storage.record_rejection(goal.goal_id, fingerprint, reason)
        self.get_logger().warn(
            f"reject goal id={goal.goal_id} reason={reason} "
            f"existing_state={states.name(existing.state)} mismatch={mismatch}"
        )
        return rclpy.action.GoalResponse.REJECT

    def _cancel_cb(self, goal_handle):
        # rclpy passes the ServerGoalHandle of the targeted goal; its
        # goal_id is the ROS action UUID. Map back to our client-chosen id.
        goal_id = self._goal_id_from_handle_uuid(goal_handle.goal_id)
        if goal_id is None:
            self.get_logger().warn("cancel for unknown handle")
            return rclpy.action.CancelResponse.REJECT

        status, observed = self._storage.request_cancel(goal_id)
        if status == "canceled":
            with self._active_lock:
                active = self._active.get(goal_id)
            if active is not None:
                active.event.set()
            self.get_logger().info(f"cancel admitted id={goal_id}")
            return rclpy.action.CancelResponse.ACCEPT
        if observed == states.CANCELED:
            # Idempotent re-cancel.
            return rclpy.action.CancelResponse.ACCEPT
        self.get_logger().warn(
            f"cancel rejected id={goal_id} state={states.name(observed)}"
        )
        return rclpy.action.CancelResponse.REJECT

    def _goal_id_from_handle_uuid(self, goal_uuid) -> Optional[str]:
        """Map a ROS action goal-uuid back to our client-chosen goal id."""
        target = _uuid_bytes(goal_uuid)
        with self._active_lock:
            for gid, active in self._active.items():
                if active.handle is None:
                    continue
                if _uuid_bytes(active.handle.goal_id) == target:
                    return gid
        return None

    # ============================================================== execution
    def _execute_cb(self, goal_handle: ServerGoalHandle):
        goal = goal_handle.request
        goal_id = goal.goal_id
        event = threading.Event()
        with self._active_lock:
            self._active[goal_id] = _Active(
                goal_id=goal_id,
                handle=goal_handle,
                event=event,
                started_at=time.time(),
            )
        try:
            return self._run_goal(goal_id, goal_handle, event)
        finally:
            with self._active_lock:
                cur = self._active.get(goal_id)
                if cur is not None and cur.handle is goal_handle:
                    self._active.pop(goal_id, None)

    def _run_goal(
        self,
        goal_id: str,
        handle: Optional[ServerGoalHandle],
        event: threading.Event,
    ) -> SegmentedTask.Result:
        """Single execution loop shared by live goals and headless resumes."""
        # PENDING -> RUNNING (fresh goal). A resume already flipped
        # RECOVERABLE -> RUNNING before this is called.
        self._storage.mark_running(goal_id)

        g = self._storage.get_goal(goal_id)
        total = g.total_segments
        iterations = g.iterations
        inputs = list(g.inputs)

        done = self._storage.get_segments(goal_id)
        idx = len(done)
        prev_chain = done[-1].chained_hash if done else CHAIN_GENESIS

        def publish(feedback_fields: tuple[int, int, str, str]) -> None:
            if handle is None or not handle.is_active:
                return
            seg_idx, completed, seg_hash, chained = feedback_fields
            fb = SegmentedTask.Feedback(
                goal_id=goal_id,
                segment_index=seg_idx,
                completed_segments=completed,
                segment_hash=seg_hash,
                chained_hash=chained,
            )
            handle.publish_feedback(fb)

        def settle() -> SegmentedTask.Result:
            row = self._storage.get_goal(goal_id)
            result = SegmentedTask.Result(
                goal_id=goal_id,
                terminal_state=row.state,
                completed_segments=row.completed_segments,
                total_segments=row.total_segments,
                result_hash=row.result_hash,
                error_code=row.error_code,
            )
            if handle is not None and handle.is_active:
                if row.state == states.SUCCEEDED:
                    handle.succeed(result)
                elif row.state == states.CANCELED:
                    # Our cancel callback commits the durable CANCELED state
                    # and returns ACCEPT; rclpy then performs EXECUTING ->
                    # CANCELING asynchronously. The execute loop may notice
                    # the durable change and reach this settle *before* that
                    # rcl transition lands, in which case calling canceled()
                    # from EXECUTING is illegal. Wait until the handle is
                    # CANCELING (is_cancel_requested flips true), bounded so
                    # a lost rcl transition cannot hang the thread.
                    if handle is not None:
                        waited = 0.0
                        while (
                            not handle.is_cancel_requested
                            and handle.is_active
                            and waited < 2.0
                        ):
                            time.sleep(0.005)
                            waited += 0.005
                        # Defensive: if rclpy never performed
                        # EXECUTING -> CANCELING (it normally does right after
                        # our cancel callback), do that single transition now.
                        # It is safe here precisely because it did not happen
                        # yet, so this is not a double CANCEL_GOAL.
                        if (
                            handle.is_active
                            and not handle.is_cancel_requested
                            and handle.status == GoalStatus.STATUS_EXECUTING
                        ):
                            handle._update_state(
                                _rclpy_bindings.GoalEvent.CANCEL_GOAL
                            )
                    # When our cancel callback returned ACCEPT, rclpy already
                    # moved the handle EXECUTING -> CANCELING itself; the only
                    # remaining action is the single terminal CANCELED event.
                    handle.canceled(result)
                else:
                    handle.abort(result)
            return result

        while idx < total:
            # rclpy sets this once our cancel callback accepted and it has
            # driven EXECUTING -> CANCELING; the durable CANCELED state was
            # committed by the callback, so stop producing segments.
            if handle is not None and handle.is_cancel_requested:
                break
            if event.is_set():
                break
            cur = self._storage.get_goal(goal_id)
            if cur.state != states.RUNNING:
                break

            # Pace the (simulated) work; wake promptly on cancel so that
            # "cancel during the last segment" is race-free from the client
            # perspective when segment_delay is configured.
            if self._segment_delay > 0:
                if event.wait(self._segment_delay):
                    break
                if self._storage.get_goal(goal_id).state != states.RUNNING:
                    break

            t0 = time.perf_counter()
            seg_hash = crypto.segment_digest(
                goal_id, idx, inputs[idx], iterations
            )
            chained = crypto.chain_step(prev_chain, seg_hash)
            duration_ms = (time.perf_counter() - t0) * 1000.0

            # Persist FIRST, publish second: a feedback can never claim more
            # than what is durable.
            res: CommitResult = self._storage.commit_segment(
                goal_id=goal_id,
                segment_index=idx,
                segment_hash=seg_hash,
                chained_hash=chained,
                input_fingerprint=crypto.input_fingerprint(inputs[idx]),
                duration_ms=duration_ms,
                recovered=False,
            )
            if res.status == "rejected":
                self.get_logger().info(
                    f"segment {idx} of {goal_id} not stored: "
                    f"goal state={states.name(res.state)}"
                )
                break

            publish((idx, res.completed_segments, seg_hash, chained))
            if res.state == states.SUCCEEDED:
                return settle()
            prev_chain = chained
            idx += 1

        return settle()

    # ============================================================== recovery
    def _startup_recovery(self) -> None:
        self._recovery_timer.cancel()
        try:
            recoverable = self._storage.mark_all_running_recoverable()
        except Exception:  # pragma: no cover - defensive
            self.get_logger().exception("startup sweep failed")
            return
        if not recoverable:
            return
        self.get_logger().info(
            f"{len(recoverable)} goal(s) marked RECOVERABLE after restart"
        )
        for goal_id in recoverable:
            self._recover_one(goal_id)

    def _recover_one(self, goal_id: str) -> None:
        g = self._storage.get_goal(goal_id)
        if g is None or g.state != states.RECOVERABLE:
            return

        segments = self._storage.get_segments(goal_id)

        # Explicit policy: ABORT -> terminal ABORTED, never resumed.
        if g.policy == states.POLICY_ABORT:
            self._storage.recover_abort(goal_id)
            self.get_logger().warn(
                f"goal {goal_id} aborted on recovery by POLICY_ABORT "
                f"(completed={g.completed_segments}/{g.total_segments})"
            )
            return

        # Explicit policy: RESUME. Verify every persisted segment by
        # recomputing the real crypto from the stored inputs; a mismatch
        # means the durable record is untrustworthy and the goal is aborted.
        if not self._verify_integrity(g, segments):
            self._storage.abort(goal_id, states.ERR_INTEGRITY)
            self.get_logger().error(
                f"goal {goal_id} aborted: recovery integrity check failed"
            )
            return

        changed, obs = self._storage.recover_begin(goal_id)
        if not changed:
            self.get_logger().warn(
                f"goal {goal_id} recovery lost race, state={states.name(obs)}"
            )
            return
        self._storage.mark_segments_recovered(goal_id)
        self.get_logger().info(
            f"resuming goal {goal_id} at segment {len(segments)}/"
            f"{g.total_segments}"
        )

        event = threading.Event()
        with self._active_lock:
            self._active[goal_id] = _Active(
                goal_id=goal_id, handle=None, event=event,
                started_at=time.time(),
            )

        def headless() -> None:
            try:
                result = self._run_goal(goal_id, None, event)
                self.get_logger().info(
                    f"headless resume finished id={goal_id} "
                    f"terminal={states.name(result.terminal_state)} "
                    f"completed={result.completed_segments}"
                )
            finally:
                with self._active_lock:
                    cur = self._active.get(goal_id)
                    if cur is not None and cur.handle is None:
                        self._active.pop(goal_id, None)

        threading.Thread(
            target=headless, name=f"resume-{goal_id}", daemon=True
        ).start()

    def _verify_integrity(self, g: GoalRow, rows) -> bool:
        """Recompute every segment hash and chain link from stored inputs."""
        if len(rows) > g.total_segments:
            return False
        prev = CHAIN_GENESIS
        for i, row in enumerate(rows):
            if row.segment_index != i:
                return False
            if i >= len(g.inputs):
                return False
            expect_seg = crypto.segment_digest(
                g.goal_id, i, g.inputs[i], g.iterations
            )
            if not crypto.constant_time_equal(expect_seg, row.segment_hash):
                return False
            if not crypto.constant_time_equal(
                crypto.input_fingerprint(g.inputs[i]), row.input_fingerprint
            ):
                return False
            expect_chain = crypto.chain_step(prev, expect_seg)
            if not crypto.constant_time_equal(expect_chain, row.chained_hash):
                return False
            prev = row.chained_hash
        return True

    # ============================================================ history API
    def _get_goal_cb(self, request, response):
        g = self._storage.get_goal(request.goal_id)
        if g is None:
            response.exists = False
            response.goal_id = request.goal_id
            return response
        response.exists = True
        response.goal_id = g.goal_id
        response.state = g.state
        response.completed_segments = g.completed_segments
        response.total_segments = g.total_segments
        response.fingerprint = g.fingerprint
        response.recovery_policy = g.policy
        response.result_hash = g.result_hash
        response.error_code = g.error_code or g.last_rejection_code
        response.segments = [
            SegmentRecord(
                goal_id=r.goal_id,
                segment_index=r.segment_index,
                segment_hash=r.segment_hash,
                chained_hash=r.chained_hash,
                input_fingerprint=r.input_fingerprint,
                duration_ms=r.duration_ms,
                recovered=r.recovered,
            )
            for r in self._storage.get_segments(g.goal_id)
        ]
        return response

    def _list_goals_cb(self, request, response):
        rows = self._storage.list_goals(request.state_filter)
        response.goals = [
            GoalSummary(
                goal_id=g.goal_id,
                state=g.state,
                completed_segments=g.completed_segments,
                total_segments=g.total_segments,
                recovery_policy=g.policy,
                fingerprint=g.fingerprint,
                result_hash=g.result_hash,
                error_code=g.error_code or g.last_rejection_code,
            )
            for g in rows
        ]
        return response


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Segmented task action server")
    parser.add_argument("--db-path", default="data/segments.db")
    parser.add_argument("--action-name", default="segmented_task")
    parser.add_argument(
        "--segment-delay",
        type=float,
        default=0.05,
        help="seconds of (paced, cancel-interruptible) work per segment",
    )
    args, _ = parser.parse_known_args(argv)
    return args


def main(argv=None) -> None:
    args = _parse_args(argv)
    rclpy.init(args=argv)
    node = SegmentTaskServer(
        db_path=args.db_path,
        action_name=args.action_name,
        segment_delay_s=args.segment_delay,
    )
    executor = MultiThreadedExecutor(num_threads=8)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        executor.shutdown()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
