"""End-to-end tests with real rclpy action client + server in one process.

These exercise the full protocol stack (DDS discovery, goal/cancel/result
services, feedback topics, history services) backed by a real SQLite file,
using the MultiThreadedExecutor the production server ships with.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

import rclpy
from action_msgs.srv import CancelGoal
from rclpy.action import ActionClient
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor, SingleThreadedExecutor

from action_cancel_interfaces.action import SegmentedTask
from action_cancel_interfaces.srv import GetGoal, ListGoals
from action_cancel_server import states
from action_cancel_server.server import SegmentTaskServer

ITERS = 200  # cheap real crypto; delay dominates segment timing


@pytest.fixture()
def ros_ctx():
    os.environ.setdefault("ROS_LOCALHOST_ONLY", "1")
    if not rclpy.ok():
        rclpy.init()
    yield
    if rclpy.ok():
        rclpy.shutdown()


@pytest.fixture()
def stack(ros_ctx, tmp_path, request):
    action_name = "test_" + uuid.uuid4().hex[:10]
    db = str(tmp_path / "e2e.db")
    delay = getattr(request, "param", 0.15)
    server = SegmentTaskServer(
        db_path=db, action_name=action_name, segment_delay_s=delay
    )
    srv_exec = MultiThreadedExecutor(num_threads=8)
    srv_exec.add_node(server)

    import threading

    t = threading.Thread(target=srv_exec.spin, daemon=True)
    t.start()
    # allow the startup recovery timer to fire once (no goals -> no-op)
    time.sleep(0.15)

    client_node = rclpy.create_node("e2e_client")
    cli_exec = SingleThreadedExecutor()
    cli_exec.add_node(client_node)
    tc = threading.Thread(target=cli_exec.spin, daemon=True)
    tc.start()

    ac = ActionClient(client_node, SegmentedTask, action_name,
                      callback_group=ReentrantCallbackGroup())
    get_cli = client_node.create_client(GetGoal, action_name + "/get_goal")
    list_cli = client_node.create_client(ListGoals, action_name + "/list_goals")
    assert ac.wait_for_server(timeout_sec=10.0)

    yield {
        "node": client_node,
        "action": ac,
        "get": get_cli,
        "list": list_cli,
        "server": server,
        "db": db,
        "action_name": action_name,
    }

    srv_exec.shutdown()
    cli_exec.shutdown()
    server.destroy_node()
    client_node.destroy_node()


def _goal(goal_id, inputs=None, iters=ITERS, policy=states.POLICY_RESUME):
    return SegmentedTask.Goal(
        goal_id=goal_id,
        segment_inputs=inputs if inputs is not None else ["a", "b", "c"],
        pbkdf2_iterations=iters,
        recovery_policy=policy,
    )


def _wait(future, timeout=20.0):
    """Wait for a future serviced by the background client executor."""
    end = time.time() + timeout
    while not future.done() and time.time() < end:
        time.sleep(0.02)
    return future.done()


def _send(stack, goal, feedbacks=None):
    """Send goal and wait for acceptance."""
    def fb_cb(msg):
        if feedbacks is not None:
            feedbacks.append(msg.feedback)

    fut = stack["action"].send_goal_async(goal, feedback_callback=fb_cb)
    assert _wait(fut)
    return fut.result()


def _result(stack, gh, timeout=30.0):
    fut = gh.get_result_async()
    assert _wait(fut, timeout)
    return fut.result()


def _get_goal(stack, goal_id):
    fut = stack["get"].call_async(GetGoal.Request(goal_id=goal_id))
    assert _wait(fut, 10.0)
    return fut.result()


# --------------------------------------------------------------------- tests
def test_happy_path_matches_durable_history(stack):
    feedbacks = []
    gh = _send(stack, _goal("ok-1"), feedbacks)
    assert gh.accepted
    wrapped = _result(stack, gh)
    res = wrapped.result
    assert res.terminal_state == states.SUCCEEDED
    assert res.completed_segments == 3

    # one feedback per segment, in order, counts monotonic
    assert [f.segment_index for f in feedbacks] == [0, 1, 2]
    assert [f.completed_segments for f in feedbacks] == [1, 2, 3]

    rec = _get_goal(stack, "ok-1")
    assert rec.exists and rec.state == states.SUCCEEDED
    assert rec.completed_segments == 3
    assert len(rec.segments) == 3
    # result hash equals the last chained hash, and chain recomputes
    assert rec.result_hash == rec.segments[-1].chained_hash == res.result_hash
    assert rec.segments[0].recovered is False


def test_duplicate_same_id_active_and_terminal_rejected(stack):
    gh1 = _send(stack, _goal("dup-1"))
    assert gh1.accepted
    _result(stack, gh1)

    # exact resend of a terminal goal: rejected, not re-run
    gh2 = _send(stack, _goal("dup-1"))
    assert not gh2.accepted

    # rejection is auditable in history
    rec = _get_goal(stack, "dup-1")
    assert rec.state == states.SUCCEEDED
    assert rec.error_code == states.ERR_DUP_FINISHED


def test_same_id_different_parameters_rejected_and_original_runs(stack):
    gh1 = _send(stack, _goal("chg-1", ["a", "b", "c"], ITERS,
                             states.POLICY_RESUME))
    assert gh1.accepted
    wrapped = _result(stack, gh1)
    assert wrapped.result.terminal_state == states.SUCCEEDED

    for variant in [
        _goal("chg-1", ["a", "b", "X"], ITERS, states.POLICY_RESUME),
        _goal("chg-1", ["a", "b"], ITERS, states.POLICY_RESUME),
        _goal("chg-1", ["a", "b", "c"], ITERS + 1, states.POLICY_RESUME),
        _goal("chg-1", ["a", "b", "c"], ITERS, states.POLICY_ABORT),
    ]:
        gh = _send(stack, variant)
        assert not gh.accepted

    rec = _get_goal(stack, "chg-1")
    # original goal untouched: 3 segments, succeeded
    assert rec.state == states.SUCCEEDED
    assert rec.total_segments == 3
    assert len(rec.segments) == 3
    assert rec.error_code == states.ERR_CHANGED_PARAMS


@pytest.mark.parametrize("stack", [1.0], indirect=True)
def test_cancel_during_last_segment_single_terminal_state(stack):
    goal_id = "cancel-last"
    feedbacks = []
    # Server paces each segment by 1.0s: after feedback #3 (penultimate)
    # the cancel has a full second to be admitted before the last commit.
    gh = _send(
        stack,
        _goal(goal_id, ["s0", "s1", "s2", "s3", "s4"]),
        feedbacks,
    )
    assert gh.accepted

    end = time.time() + 15.0
    while len(feedbacks) < 4 and time.time() < end:
        time.sleep(0.01)
    assert len(feedbacks) == 4
    # cancel races the last segment immediately, inside its work window
    cancel_fut = gh.cancel_goal_async()
    assert _wait(cancel_fut, 5.0)
    cancel_resp = cancel_fut.result()
    # CancelGoal return code: ERROR_NONE=0 means accepted; the goal must
    # also be present in goals_canceling.
    assert cancel_resp.return_code == CancelGoal.Response.ERROR_NONE
    assert len(cancel_resp.goals_canceling) == 1

    wrapped = _result(stack, gh)
    res = wrapped.result
    assert res.terminal_state == states.CANCELED

    # terminal state == actually completed durable work
    rec = _get_goal(stack, goal_id)
    assert rec.state == states.CANCELED
    assert rec.completed_segments == res.completed_segments == len(rec.segments)
    # exactly the first four segments are durable; the last never landed
    assert rec.completed_segments == 4
    assert rec.result_hash == ""

    # a goal already canceled stays in one terminal state; a second cancel
    # request cannot move it anywhere else.
    cancel2 = gh.cancel_goal_async()
    _wait(cancel2, 5.0)
    assert cancel2.done()
    rec_after = _get_goal(stack, goal_id)
    assert rec_after.state == states.CANCELED
    assert rec_after.completed_segments == 4


def test_feedback_loss_terminal_truth_still_correct(stack):
    """Client ignores every feedback; result + history must still be exact."""
    gh = _send(stack, _goal("noloss", ["x", "y", "z", "w"]), feedbacks=None)
    assert gh.accepted
    wrapped = _result(stack, gh)
    res = wrapped.result
    assert res.terminal_state == states.SUCCEEDED
    assert res.completed_segments == 4
    rec = _get_goal(stack, "noloss")
    assert rec.state == states.SUCCEEDED
    assert len(rec.segments) == 4
    assert rec.result_hash == res.result_hash


def test_invalid_goals_rejected(stack):
    bad = [
        _goal("", ["a"]),
        _goal("bad-empty", []),
        _goal("bad-iter", ["a"], iters=0),
        _goal("bad-pol", ["a"], policy=99),
    ]
    for g in bad:
        assert not _send(stack, g).accepted


def test_recovery_resume_policy_after_simulated_crash(stack, tmp_path):
    """Seed a RUNNING goal with 2 durable segments, then start a *fresh*
    server on the same DB: it must mark RECOVERABLE and resume headlessly
    to SUCCEEDED (POLICY_RESUME)."""
    from action_cancel_server.storage import Storage
    from action_cancel_server import crypto

    db = stack["db"]
    action_name = stack["action_name"]
    gid = "crash-resume"
    inputs = ["p0", "p1", "p2", "p3"]
    s = Storage(db)
    fp = crypto.goal_fingerprint(gid, inputs, ITERS, states.POLICY_RESUME)
    s.create_goal(gid, inputs, ITERS, states.POLICY_RESUME, fp)
    s.mark_running(gid)
    prev = crypto.CHAIN_GENESIS
    for i in range(2):
        seg = crypto.segment_digest(gid, i, inputs[i], ITERS)
        chained = crypto.chain_step(prev, seg)
        r = s.commit_segment(gid, i, seg, chained,
                             crypto.input_fingerprint(inputs[i]), 1.0)
        assert r.status == "committed"
        prev = chained
    s.close()

    # spin up a second server on the same DB (simulates process restart)
    server2 = SegmentTaskServer(
        db_path=db, action_name=action_name + "_r2", segment_delay_s=0.05
    )
    ex2 = MultiThreadedExecutor(num_threads=6)
    ex2.add_node(server2)
    import threading
    t = threading.Thread(target=ex2.spin, daemon=True)
    t.start()
    try:
        # wait for headless resume to finish
        end = time.time() + 20.0
        rec = None
        while time.time() < end:
            rec = _get_goal(stack, gid)
            if rec.exists and rec.state in states.TERMINAL:
                break
            time.sleep(0.1)
        assert rec is not None and rec.state == states.SUCCEEDED
        assert rec.completed_segments == 4
        assert len(rec.segments) == 4
        # the pre-crash rows are flagged recovered
        assert all(sg.recovered for sg in rec.segments[:2])
    finally:
        ex2.shutdown()
        server2.destroy_node()


def test_recovery_abort_policy_marks_terminal(stack):
    from action_cancel_server.storage import Storage
    from action_cancel_server import crypto

    db = stack["db"]
    action_name = stack["action_name"]
    gid = "crash-abort"
    inputs = ["q0", "q1", "q2"]
    s = Storage(db)
    fp = crypto.goal_fingerprint(gid, inputs, ITERS, states.POLICY_ABORT)
    s.create_goal(gid, inputs, ITERS, states.POLICY_ABORT, fp)
    s.mark_running(gid)
    seg = crypto.segment_digest(gid, 0, inputs[0], ITERS)
    chained = crypto.chain_step(crypto.CHAIN_GENESIS, seg)
    s.commit_segment(gid, 0, seg, chained,
                     crypto.input_fingerprint(inputs[0]), 1.0)
    s.close()

    server2 = SegmentTaskServer(
        db_path=db, action_name=action_name + "_r3", segment_delay_s=0.05
    )
    ex2 = MultiThreadedExecutor(num_threads=4)
    ex2.add_node(server2)
    import threading
    t = threading.Thread(target=ex2.spin, daemon=True)
    t.start()
    try:
        end = time.time() + 10.0
        rec = None
        while time.time() < end:
            rec = _get_goal(stack, gid)
            if rec.exists and rec.state in states.TERMINAL:
                break
            time.sleep(0.1)
        assert rec is not None
        assert rec.state == states.ABORTED
        assert rec.error_code == states.ERR_ABORT_POLICY
        # completed work is preserved for inspection, not deleted
        assert rec.completed_segments == 1
        assert len(rec.segments) == 1
    finally:
        ex2.shutdown()
        server2.destroy_node()


def test_list_goals_history_query(stack):
    _result(stack, _send(stack, _goal("list-1", ["a"])))
    fbs = []
    gh = _send(stack, _goal("list-2", ["a", "b"]), fbs)
    # cancel only after segment 0 is durably done, avoiding a race with
    # completion of the first segment
    end = time.time() + 10.0
    while not fbs and time.time() < end:
        time.sleep(0.02)
    assert fbs
    cancel_fut = gh.cancel_goal_async()
    _wait(cancel_fut, 5.0)
    wrapped = _result(stack, gh)
    assert wrapped.result.terminal_state == states.CANCELED

    fut = stack["list"].call_async(ListGoals.Request(state_filter=255))
    assert _wait(fut, 10.0)
    goals = {g.goal_id: g for g in fut.result().goals}
    assert "list-1" in goals and goals["list-1"].state == states.SUCCEEDED
    assert goals["list-2"].state == states.CANCELED

    fut2 = stack["list"].call_async(
        ListGoals.Request(state_filter=states.SUCCEEDED))
    assert _wait(fut2, 10.0)
    ids = {g.goal_id for g in fut2.result().goals}
    assert "list-1" in ids and "list-2" not in ids
