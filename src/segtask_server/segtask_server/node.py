"""rclpy node: segmented task action server.

Responsibilities:
* expose the ``~/segment_task`` action (segtask_msgs/SegmentTask);
* expose query/list/admin services so outcome history survives client
  disconnects and restarts;
* on startup, mark goals left running by previous processes RECOVERING and
  continue or stop them per the explicit recovery mode/goal policy;
* funnel all concurrency decisions through :mod:`segtask_server.db`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import threading
from types import SimpleNamespace
from typing import Optional

import rclpy
from rclpy.action import ActionServer
from rclpy.action.server import (CancelResponse, GoalResponse, ServerGoalHandle)
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node

from segtask_msgs.action import SegmentTask
from segtask_msgs.msg import GoalInfo
from segtask_msgs.srv import AdminRequest, ListGoals, QueryGoal

from . import status as st
from .crypto import canonical_payload
from .db import Store
from .runner import GoalExecutor

logger = logging.getLogger('segtask')


def params_hash(segments_total: int, work_units: int, checkpoint_ms: int,
                policy: str) -> str:
    payload = {'segments_total': segments_total, 'work_units': work_units,
               'checkpoint_ms': checkpoint_ms, 'policy': policy}
    return hashlib.sha256(canonical_payload(payload).encode('utf-8')).hexdigest()


class SegTaskNode(Node):

    def __init__(self):
        super().__init__('segtask_server')

        self.declare_parameter('db_path', 'segtask_data/segtask.db')
        self.declare_parameter('recovery_mode', st.POLICY_RESUME)
        self.declare_parameter('feedback_drop_rate', 0.0)
        self.declare_parameter('feedback_drop_seed', -1)

        db_path = os.path.abspath(self.get_parameter('db_path').value)
        self.recovery_mode = str(self.get_parameter('recovery_mode').value)
        if self.recovery_mode not in st.VALID_MODES:
            raise ValueError(f'invalid recovery_mode {self.recovery_mode!r}; '
                             f'want one of {st.VALID_MODES}')
        drop_rate = float(self.get_parameter('feedback_drop_rate').value)
        if not 0.0 <= drop_rate <= 1.0:
            raise ValueError('feedback_drop_rate must be in [0, 1]')
        drop_seed = int(self.get_parameter('feedback_drop_seed').value)
        rng = random.Random(drop_seed if drop_seed >= 0 else None)

        os.makedirs(os.path.dirname(db_path) or '.', exist_ok=True)
        from .crypto import load_or_create_secret
        self._secret = load_or_create_secret(db_path + '.key')
        self.store = Store(db_path, self._secret)

        self._executor_core = GoalExecutor(
            self.store,
            publish_feedback=self._route_feedback,
            feedback_drop_rate=drop_rate,
            drop_rng=rng)
        self._goal_handles: dict[str, ServerGoalHandle] = {}
        self._handles_lock = threading.Lock()

        cb_group = ReentrantCallbackGroup()
        self._action = ActionServer(
            self, SegmentTask, '~/segment_task',
            execute_callback=self.execute_cb,
            goal_callback=self.goal_cb,
            cancel_callback=self.cancel_cb,
            callback_group=cb_group)

        self.create_service(QueryGoal, '~/query_goal', self.query_goal_cb,
                            callback_group=cb_group)
        self.create_service(ListGoals, '~/list_goals', self.list_goals_cb,
                            callback_group=cb_group)
        self.create_service(AdminRequest, '~/admin', self.admin_cb,
                            callback_group=cb_group)

        self.store.append_server_event('SERVER_STARTED', {
            'recovery_mode': self.recovery_mode,
            'run_id': self.store.run_id,
            'feedback_drop_rate': drop_rate})
        self._recover_on_startup()

        self.get_logger().info(
            f'segtask action server ready (db={db_path}, '
            f'mode={self.recovery_mode}, run_id={self.store.run_id}, '
            f'feedback_drop_rate={drop_rate})')

    # ------------------------------------------------------------- feedback

    def _route_feedback(self, goal_id: str, msg) -> None:
        with self._handles_lock:
            handle = self._goal_handles.get(goal_id)
        if handle is None or not handle.is_active:
            return
        if not isinstance(msg, SegmentTask.Feedback):
            return
        try:
            handle.publish_feedback(msg)
        except Exception:  # feedback loss must never affect execution
            logger.exception('[%s] feedback publish failed; continuing', goal_id)

    # ------------------------------------------------------------- admission

    def goal_cb(self, request) -> GoalResponse:
        g = request
        if g.segments_total <= 0 or g.work_units < 0 or g.checkpoint_ms < 1:
            logger.warning('goal %r rejected: invalid parameters (segments=%s, '
                           'work=%s, checkpoint=%s)', g.goal_id,
                           g.segments_total, g.work_units, g.checkpoint_ms)
            return GoalResponse.REJECT
        policy = g.policy or st.POLICY_RESUME
        if policy not in st.VALID_POLICIES:
            logger.warning('goal %r rejected: invalid policy %r',
                           g.goal_id, policy)
            return GoalResponse.REJECT

        ph = params_hash(g.segments_total, g.work_units, g.checkpoint_ms, policy)
        row, created = self.store.register_goal(
            g.goal_id, g.segments_total, g.work_units, g.checkpoint_ms,
            policy, ph)

        if created:
            logger.info('[%s] goal accepted (%d segments, %d units/seg)',
                        g.goal_id, g.segments_total, g.work_units)
            return GoalResponse.ACCEPT

        if row['params_hash'] != ph:
            logger.warning(
                '[%s] goal REJECTED: id already exists with different parameters '
                '(stored total=%s work=%s checkpoint=%s policy=%s)',
                g.goal_id, row['segments_total'], row['work_units'],
                row['checkpoint_ms'], row['policy'])
            return GoalResponse.REJECT
        if row['status'] in st.TERMINAL_STATUSES:
            logger.info('[%s] goal accepted as REPLAY of terminal %s result',
                        g.goal_id, st.STATUS_NAMES[row['status']])
            return GoalResponse.ACCEPT
        logger.warning('[%s] goal REJECTED: same id is already active (%s)',
                       g.goal_id, st.STATUS_NAMES[row['status']])
        return GoalResponse.REJECT

    def cancel_cb(self, goal_handle: ServerGoalHandle) -> CancelResponse:
        # rclpy passes the ServerGoalHandle of the goal being canceled.
        goal_id = goal_handle.request.goal_id
        ok, message = self.store.request_cancel(goal_id)
        if ok:
            logger.info('[%s] cancel accepted: %s', goal_id, message)
            return CancelResponse.ACCEPT
        logger.warning('[%s] cancel rejected: %s', goal_id, message)
        return CancelResponse.REJECT

    # -------------------------------------------------------------- execute

    def execute_cb(self, handle: ServerGoalHandle):
        goal = handle.request
        goal_id = goal.goal_id
        with self._handles_lock:
            self._goal_handles[goal_id] = handle
        try:
            # NOTE: rclpy's default handle_accepted_callback already calls
            # handle.execute(); never call it again here (illegal transition).
            outcome = self._executor_core.run(
                goal_id, SegmentTask.Feedback)
            return self._answer_action(handle, outcome)
        finally:
            with self._handles_lock:
                self._goal_handles.pop(goal_id, None)

    def _answer_action(self, handle: ServerGoalHandle, outcome: dict):
        result = SegmentTask.Result()
        result.code = int(outcome['code'])
        result.success = bool(outcome['success'])
        result.goal_id = outcome['goal_id']
        result.status = int(outcome['status'])
        result.segments_done = int(outcome['segments_done'])
        result.segments_total = int(outcome['segments_total'])
        result.result_hash = outcome['result_hash'] or ''
        result.message = outcome['message'] or ''
        dropped = self._executor_core.dropped_feedback.get(outcome['goal_id'], 0)
        result.feedback_dropped = int(dropped)

        if outcome['status'] == st.SUCCEEDED:
            handle.succeed()
        elif outcome['status'] == st.CANCELED:
            handle.canceled()
        else:
            handle.abort()
        logger.info('[%s] terminal: %s (%d/%d segments)%s',
                    outcome['goal_id'], st.STATUS_NAMES.get(outcome['status']),
                    outcome['segments_done'], outcome['segments_total'],
                    f" - {outcome['message']}" if outcome['message'] else '')
        return result

    # ------------------------------------------------------------- recovery

    def _recover_on_startup(self) -> None:
        recovered = self.store.startup_recovery(self.recovery_mode)
        if not recovered:
            return
        if self.recovery_mode == st.MODE_MANUAL:
            self.get_logger().warning(
                f'manual recovery mode: {len(recovered)} goal(s) parked '
                'RECOVERING, issue an ~/admin request to recover or abort')
            return
        for row in recovered:
            self._spawn_recovery(row['goal_id'])

    def _spawn_recovery(self, goal_id: str) -> None:
        def _job():
            try:
                done = self.store.get_goal(goal_id)['segments_done']
                self.get_logger().info(
                    f'[{goal_id}] resuming from segments_done={done} after restart')
                outcome = self._executor_core.run(
                    goal_id, lambda: SimpleNamespace(
                        goal_id='', segment_index=0, segments_done=0,
                        segments_total=0, status=0, last_segment_hash=''))
                self.get_logger().info(
                    f"[{goal_id}] recovery finished: "
                    f"{st.STATUS_NAMES.get(outcome['status'])} "
                    f"({outcome['segments_done']}/{outcome['segments_total']})")
            except Exception:
                logger.exception(f'[{goal_id}] recovery thread failed')

        t = threading.Thread(target=_job, name=f'recover-{goal_id}',
                             daemon=True)
        t.start()

    # ------------------------------------------------------------- services

    def query_goal_cb(self, request, response):
        row = self.store.get_goal(request.goal_id)
        if row is None:
            response.found = False
            response.goal_id = request.goal_id
            return response
        response.found = True
        self._fill_query(response, row)
        ok, err = self.verify_chain(request.goal_id)
        response.chain_ok = ok
        response.chain_error = err or ''
        return response

    def _fill_query(self, response, row) -> None:
        response.goal_id = row['goal_id']
        response.status = int(row['status'])
        response.segments_done = int(row['segments_done'])
        response.segments_total = int(row['segments_total'])
        response.policy = row['policy']
        response.resumed_after_restart = bool(row['resumed_after_restart'])
        response.params_json = json.dumps({
            'segments_total': row['segments_total'],
            'work_units': row['work_units'],
            'checkpoint_ms': row['checkpoint_ms'],
            'params_hash': row['params_hash']})
        response.result_hash = row['result_hash'] or ''
        response.result_message = row['result_message'] or ''
        response.created_at = int(row['created_at_ns'])
        response.updated_at = int(row['updated_at_ns'])
        for field, ns in (('started_at', row['started_at_ns']),
                          ('ended_at', row['ended_at_ns'])):
            stamp = getattr(response, field)
            if ns:
                stamp.sec = int(ns // 1_000_000_000)
                stamp.nanosec = int(ns % 1_000_000_000)

    def list_goals_cb(self, request, response):
        rows = self.store.list_goals(bool(request.include_finished),
                                     int(request.limit or 200))
        infos = []
        for row in rows:
            info = GoalInfo()
            info.goal_id = row['goal_id']
            info.status = int(row['status'])
            info.segments_done = int(row['segments_done'])
            info.segments_total = int(row['segments_total'])
            info.policy = row['policy']
            info.resumed_after_restart = bool(row['resumed_after_restart'])
            info.result_hash = row['result_hash'] or ''
            info.result_message = row['result_message'] or ''
            info.created_at = int(row['created_at_ns'])
            info.updated_at = int(row['updated_at_ns'])
            infos.append(info)
        response.goals = infos
        response.success = True
        response.message = f'{len(infos)} goal(s)'
        return response

    def admin_cb(self, request, response):
        goal_id = request.goal_id
        row = self.store.get_goal(goal_id)
        if row is None:
            response.success = False
            response.accepted = False
            response.message = 'unknown goal'
            return response
        if request.command == 'abort':
            ok, msg = self.store.admin_abort(goal_id)
            response.success = ok
            response.accepted = ok
            response.message = msg
            return response
        if request.command == 'recover':
            if row['status'] != st.RECOVERING:
                response.success = False
                response.accepted = False
                response.message = (f'goal is {st.STATUS_NAMES[row["status"]]}, '
                                    'not RECOVERING')
                return response
            self._spawn_recovery(goal_id)
            response.success = True
            response.accepted = True
            response.message = 'recovery started'
            return response
        response.success = False
        response.accepted = False
        response.message = f"unknown command {request.command!r}"
        return response

    # ---------------------------------------------------------------- audit

    def verify_chain(self, goal_id: str) -> tuple[bool, Optional[str]]:
        from .audit import verify_goal_chain
        return verify_goal_chain(self.store, goal_id)

    def destroy_node(self):
        try:
            self._action.destroy()
        finally:
            super().destroy_node()


def main(argv=None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    rclpy.init(args=argv)
    node = SegTaskNode()
    try:
        executor = rclpy.executors.MultiThreadedExecutor()
        executor.add_node(node)
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()
        node.store.close()


if __name__ == '__main__':
    main()
