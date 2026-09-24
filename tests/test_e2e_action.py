"""End-to-end tests against a real rclpy action server subprocess."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time

import pytest

rclpy = pytest.importorskip('rclpy')

from rclpy.action import ActionClient  # noqa: E402

from segtask_msgs.action import SegmentTask  # noqa: E402
from segtask_msgs.srv import AdminRequest, ListGoals  # noqa: E402
from segtask_server import status as st  # noqa: E402
from segtask_server.clients import client_lib  # noqa: E402
from segtask_server.clients.client_lib import (query_goal, send_goal,  # noqa: E402
                                               wait_for_action_server)


@pytest.fixture
def ros_node(server_factory, node_env):
    sp = server_factory()
    node_env(sp.domain)
    rclpy.init()
    node = rclpy.create_node(f'e2e_{sp.domain}')
    sp.wait_ready()
    yield node, sp
    node.destroy_node()
    rclpy.shutdown()


def test_happy_path_single_terminal_succeeded(ros_node):
    node, sp = ros_node
    out = send_goal(node, 'e2e-happy', 4, 30_000, checkpoint_ms=10, timeout=30)
    assert out.accepted
    assert out.status == st.SUCCEEDED
    assert out.segments_done == 4
    info = query_goal(node, 'e2e-happy')
    assert info.found and info.status == st.SUCCEEDED
    assert info.segments_done == 4
    assert info.chain_ok


def test_duplicate_active_goal_rejected(ros_node):
    """Same id while a goal is active -> reject; different params -> reject."""
    node, sp = ros_node
    client = ActionClient(node, SegmentTask, client_lib.ACTION_NAME)
    assert wait_for_action_server(client, 15)
    goal = SegmentTask.Goal()
    goal.goal_id = 'e2e-dup'
    goal.segments_total = 6
    goal.work_units = 5_000_000  # long enough to stay active through the checks
    goal.checkpoint_ms = 10
    goal.policy = 'resume'

    goal_future = client.send_goal_async(goal)
    deadline = time.monotonic() + 15
    while not goal_future.done() and time.monotonic() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)
    first_handle = goal_future.result()
    assert first_handle.accepted
    time.sleep(1.0)  # let the active goal enter RUNNING

    try:
        # Same id, same params, active -> REJECT.
        second = send_goal(node, 'e2e-dup', 6, 5_000_000, checkpoint_ms=10,
                           timeout=20)
        assert not second.accepted
        # Same id, different params -> REJECT.
        third = send_goal(node, 'e2e-dup', 9, 10, checkpoint_ms=10, timeout=20)
        assert not third.accepted
    finally:
        # Cancel the long active goal to finish cleanly.
        cancel_fut = first_handle.cancel_goal_async()
        deadline = time.monotonic() + 20
        while not cancel_fut.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        res_fut = first_handle.get_result_async()
        deadline = time.monotonic() + 20
        while not res_fut.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        assert res_fut.done() and res_fut.result().result.status == st.CANCELED


def test_same_id_different_params_after_finish_rejected(ros_node):
    node, sp = ros_node
    first = send_goal(node, 'e2e-replay', 2, 20_000, timeout=30)
    assert first.status == st.SUCCEEDED
    replay = send_goal(node, 'e2e-replay', 2, 20_000, timeout=30)
    assert replay.accepted and replay.code == st.CODE_REPLAYED
    assert replay.result_hash == first.result_hash
    diff = send_goal(node, 'e2e-replay', 3, 20_000, timeout=30)
    assert not diff.accepted


def test_cancel_last_segment_one_terminal_state(ros_node):
    node, sp = ros_node
    out = send_goal(node, 'e2e-cancel-last', 4, 500_000, checkpoint_ms=10,
                    cancel_at_segment=3, timeout=60)
    assert out.status == st.CANCELED
    assert out.segments_done <= 4
    info = query_goal(node, 'e2e-cancel-last')
    assert info.status == st.CANCELED
    assert info.segments_done == out.segments_done
    assert info.chain_ok
    # Count stays frozen.
    frozen = info.segments_done
    time.sleep(0.8)
    info2 = query_goal(node, 'e2e-cancel-last')
    assert info2.status == st.CANCELED and info2.segments_done == frozen


def test_cancel_early(ros_node):
    node, sp = ros_node
    out = send_goal(node, 'e2e-cancel-early', 10, 200_000, checkpoint_ms=10,
                    cancel_at_segment=0, timeout=40)
    assert out.status == st.CANCELED
    assert out.segments_done < 10


def test_feedback_loss_still_succeeds(server_factory, node_env):
    sp = server_factory(drop_rate=0.7, drop_seed=7, db_name='loss.db')
    node_env(sp.domain)
    rclpy.init()
    node = rclpy.create_node('e2e_loss')
    try:
        sp.wait_ready()
        out = send_goal(node, 'e2e-loss', 8, 60_000, checkpoint_ms=10,
                        timeout=60)
        assert out.status == st.SUCCEEDED and out.segments_done == 8
        assert out.feedback_dropped_reported > 0
        assert len(out.feedbacks) < 16
        info = query_goal(node, 'e2e-loss')
        assert info.status == 4 and info.segments_done == 8 and info.chain_ok
    finally:
        node.destroy_node()
        rclpy.shutdown()


def test_crash_sigkill_then_resume(server_factory, node_env, audit_bin):
    sp = server_factory(db_name='crash.db')
    node_env(sp.domain)
    rclpy.init()
    launcher = rclpy.create_node('e2e_crash_launcher')
    sp.wait_ready()
    try:
        client = ActionClient(launcher, SegmentTask, client_lib.ACTION_NAME)
        assert wait_for_action_server(client, 15)
        goal = SegmentTask.Goal()
        goal.goal_id = 'e2e-crash-resume'
        goal.segments_total = 6
        goal.work_units = 1_500_000
        goal.checkpoint_ms = 10
        goal.policy = 'resume'
        goal_future = client.send_goal_async(goal)
        deadline = time.monotonic() + 15
        while not goal_future.done() and time.monotonic() < deadline:
            rclpy.spin_once(launcher, timeout_sec=0.1)
        assert goal_future.result().accepted

        def db_done():
            con = sqlite3.connect(sp.db_path)
            try:
                row = con.execute(
                    'SELECT status, segments_done FROM goals WHERE goal_id=?',
                    ('e2e-crash-resume',)).fetchone()
            finally:
                con.close()
            return row

        deadline = time.monotonic() + 20
        row = None
        while time.monotonic() < deadline:
            row = db_done()
            if row and 1 <= row[1] < 6:
                break
            time.sleep(0.1)
        assert row and 1 <= row[1] < 6
        sp.kill()
        assert db_done()[0] == st.RUNNING  # status at crash
    finally:
        launcher.destroy_node()
        rclpy.shutdown()

    # Restart same DB, same mode.
    from conftest import ServerProcess
    sp_restart = ServerProcess(sp.db_path, sp.domain, mode='resume',
                               log_dir=os.path.dirname(sp.db_path))
    rclpy.init()
    node = rclpy.create_node('e2e_crash_observer')
    try:
        sp_restart.wait_ready()
        info = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            info = query_goal(node, 'e2e-crash-resume', timeout=2.0)
            if info and info.found and info.status in st.TERMINAL_STATUSES:
                break
            time.sleep(0.2)
        assert info is not None and info.status == st.SUCCEEDED
        assert info.segments_done == 6
        assert info.resumed_after_restart
        assert info.chain_ok
    finally:
        node.destroy_node()
        rclpy.shutdown()
        sp_restart.terminate()

    # Offline audit recomputes all segment hashes from scratch.
    res = subprocess.run([audit_bin, '--db-path', sp.db_path],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_crash_then_abort_policy(server_factory, node_env):
    sp = server_factory(db_name='crasha.db', mode='resume')
    node_env(sp.domain)
    rclpy.init()
    launcher = rclpy.create_node('e2e_crasha_launcher')
    sp.wait_ready()
    try:
        client = ActionClient(launcher, SegmentTask, client_lib.ACTION_NAME)
        assert wait_for_action_server(client, 15)
        goal = SegmentTask.Goal()
        goal.goal_id = 'e2e-crash-abort'
        goal.segments_total = 6
        goal.work_units = 1_500_000
        goal.checkpoint_ms = 10
        goal.policy = 'abort'
        goal_future = client.send_goal_async(goal)
        deadline = time.monotonic() + 15
        while not goal_future.done() and time.monotonic() < deadline:
            rclpy.spin_once(launcher, timeout_sec=0.1)
        assert goal_future.result().accepted

        def db_row():
            con = sqlite3.connect(sp.db_path)
            try:
                return con.execute(
                    'SELECT status, segments_done FROM goals WHERE goal_id=?',
                    ('e2e-crash-abort',)).fetchone()
            finally:
                con.close()

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            row = db_row()
            if row and 1 <= row[1] < 6:
                break
            time.sleep(0.1)
        done_before = db_row()[1]
        assert 1 <= done_before < 6
        sp.kill()
    finally:
        launcher.destroy_node()
        rclpy.shutdown()

    from conftest import ServerProcess
    sp_restart = ServerProcess(sp.db_path, sp.domain, mode='resume',
                               log_dir=os.path.dirname(sp.db_path))
    rclpy.init()
    node = rclpy.create_node('e2e_crasha_observer')
    try:
        sp_restart.wait_ready()
        deadline = time.monotonic() + 20
        info = None
        while time.monotonic() < deadline:
            info = query_goal(node, 'e2e-crash-abort', timeout=2.0)
            if info and info.found and info.status in st.TERMINAL_STATUSES:
                break
            time.sleep(0.2)
        assert info.status == st.ABORTED
        assert info.segments_done == done_before  # partial work preserved
        assert info.chain_ok
    finally:
        node.destroy_node()
        rclpy.shutdown()
        sp_restart.terminate()


def test_list_goals_service_and_admin_recover(server_factory, node_env):
    sp = server_factory(db_name='manual.db', mode='manual')
    node_env(sp.domain)
    rclpy.init()
    node = rclpy.create_node('e2e_manual')
    try:
        sp.wait_ready()
        # First run: goal completes normally, giving us history.
        out = send_goal(node, 'e2e-finished', 2, 10_000, timeout=20)
        assert out.status == st.SUCCEEDED
        list_client = node.create_client(ListGoals, client_lib.LIST_SERVICE)
        assert list_client.wait_for_service(timeout_sec=5)
        req = ListGoals.Request()
        req.include_finished = True
        fut = list_client.call_async(req)
        deadline = time.monotonic() + 5
        while not fut.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        resp = fut.result()
        assert resp.success and any(g.goal_id == 'e2e-finished' for g in resp.goals)
    finally:
        node.destroy_node()
        rclpy.shutdown()

    # Kill server with no active goal, restart in manual: finished goal stays
    # terminal and is listed; admin services respond.
    sp.terminate()
    from conftest import ServerProcess
    sp2 = ServerProcess(sp.db_path, sp.domain, mode='manual',
                        log_dir=os.path.dirname(sp.db_path))
    rclpy.init()
    node = rclpy.create_node('e2e_manual2')
    try:
        sp2.wait_ready()
        info = query_goal(node, 'e2e-finished')
        assert info.status == st.SUCCEEDED  # terminal goals untouched by recovery
        admin = node.create_client(AdminRequest, client_lib.ADMIN_SERVICE)
        assert admin.wait_for_service(timeout_sec=5)
        req = AdminRequest.Request()
        req.goal_id = 'e2e-finished'
        req.command = 'abort'
        fut = admin.call_async(req)
        deadline = time.monotonic() + 5
        while not fut.done() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=0.1)
        assert not fut.result().accepted  # cannot abort terminal goal
    finally:
        node.destroy_node()
        rclpy.shutdown()
        sp2.terminate()
