"""Unit tests for the SQLite state machine (no ROS required)."""

import itertools
import threading

import pytest

from action_cancel_server import crypto, states
from action_cancel_server.storage import Storage


@pytest.fixture()
def store(tmp_path):
    s = Storage(tmp_path / "test.db")
    yield s
    s.close()


def _seg(store, gid, idx, inputs=("a", "b", "c"), iters=100, prev=None):
    seg = crypto.segment_digest(gid, idx, inputs[idx], iters)
    prev_chain = prev if prev is not None else crypto.CHAIN_GENESIS
    chained = crypto.chain_step(prev_chain, seg)
    res = store.commit_segment(
        gid, idx, seg, chained,
        crypto.input_fingerprint(inputs[idx]), 1.0,
    )
    return res, chained


def test_create_and_dedup_states(store):
    fp = crypto.goal_fingerprint("g", ["a"], 100, 0)
    status, g = store.create_goal("g", ["a"], 100, 0, fp)
    assert status == "created" and g.state == states.PENDING

    status, g = store.create_goal("g", ["a"], 100, 0, fp)
    assert status == "duplicate_active"

    store.mark_running("g")
    status, _ = store.create_goal("g", ["a"], 100, 0, fp)
    assert status == "duplicate_active"

    # terminal duplicate reports duplicate_terminal and stays unchanged
    changed, st = store.request_cancel("g")
    assert changed and st == states.CANCELED
    status, g = store.create_goal("g", ["a"], 100, 0, fp)
    assert status == "duplicate_terminal" and g.state == states.CANCELED


def test_canceled_goal_rejects_further_segments(store):
    fp = crypto.goal_fingerprint("g", ["a", "b"], 100, 0)
    store.create_goal("g", ["a", "b"], 100, 0, fp)
    store.mark_running("g")
    r, _ = _seg(store, "g", 0, ["a", "b"])
    assert r.status == "committed"

    assert store.request_cancel("g")[0] == "canceled"

    # A late result arriving after cancellation must not be stored.
    seg = crypto.segment_digest("g", 1, "b", 100)
    chained = crypto.chain_step(
        store.get_segments("g")[-1].chained_hash, seg
    )
    res = store.commit_segment(
        "g", 1, seg, chained, crypto.input_fingerprint("b"), 1.0
    )
    assert res.status == "rejected"
    assert res.state == states.CANCELED
    g = store.get_goal("g")
    assert g.state == states.CANCELED and g.completed_segments == 1
    assert len(store.get_segments("g")) == 1


def test_cancel_vs_complete_race_has_single_terminal_state(store, tmp_path):
    """The last-segment commit and the cancel transaction serialize;
    regardless of the interleaving there is exactly one terminal state.

    Two orderings are forced deterministically through two independent
    SQLite connections (the same situation as two execution threads), then
    100 rounds of genuine concurrent threads are checked for the invariants.
    """
    n_segments = 4
    inputs = [f"in{i}" for i in range(n_segments)]
    db_counter = itertools.count()

    def fresh_storage():
        return Storage(tmp_path / f"race_{next(db_counter)}.db")

    def setup_penultimate(s):
        fp = crypto.goal_fingerprint("race", inputs, 100, 0)
        s.create_goal("race", inputs, 100, 0, fp)
        s.mark_running("race")
        prev = crypto.CHAIN_GENESIS
        for i in range(n_segments - 1):
            seg = crypto.segment_digest("race", i, inputs[i], 100)
            chained = crypto.chain_step(prev, seg)
            r = s.commit_segment(
                "race", i, seg, chained,
                crypto.input_fingerprint(inputs[i]), 1.0,
            )
            assert r.status == "committed"
            prev = chained
        return prev

    def final_segment(prev_chain):
        seg = crypto.segment_digest("race", n_segments - 1, inputs[-1], 100)
        return seg, crypto.chain_step(prev_chain, seg)

    # ---- ordering 1: completion lands first, cancel loses the CAS
    s_a = fresh_storage()
    s_b = Storage(s_a.db_path)
    prev = setup_penultimate(s_a)
    seg, final_chain = final_segment(prev)
    r = s_a.commit_segment(
        "race", n_segments - 1, seg, final_chain,
        crypto.input_fingerprint(inputs[-1]), 1.0,
    )
    assert r.status == "committed" and r.state == states.SUCCEEDED
    status, observed = s_b.request_cancel("race")
    assert status == "already_terminal" and observed == states.SUCCEEDED
    g = s_a.get_goal("race")
    assert g.state == states.SUCCEEDED
    assert g.completed_segments == n_segments
    assert len(s_a.get_segments("race")) == n_segments
    assert g.result_hash == final_chain
    s_a.close(); s_b.close()

    # ---- ordering 2: cancel lands first, final result is refused
    s_a = fresh_storage()
    s_b = Storage(s_a.db_path)
    prev = setup_penultimate(s_a)
    seg, final_chain = final_segment(prev)
    status, observed = s_b.request_cancel("race")
    assert status == "canceled" and observed == states.CANCELED
    r = s_a.commit_segment(
        "race", n_segments - 1, seg, final_chain,
        crypto.input_fingerprint(inputs[-1]), 1.0,
    )
    assert r.status == "rejected" and r.state == states.CANCELED
    g = s_b.get_goal("race")
    assert g.state == states.CANCELED
    # terminal state and actually-stored work agree exactly
    assert g.completed_segments == n_segments - 1
    assert len(s_b.get_segments("race")) == n_segments - 1
    assert g.result_hash == ""
    s_a.close(); s_b.close()

    # ---- genuine concurrency: invariants must hold every round
    s = store
    prev = setup_penultimate(s)
    seg, final_chain = final_segment(prev)
    barrier = threading.Barrier(2)
    outcomes = set()

    def reset_penultimate():
        s._conn.execute(
            "DELETE FROM segments WHERE goal_id='race' "
            "AND segment_index>=?",
            (n_segments - 1,),
        )
        s._conn.execute(
            "UPDATE goals SET state=?, completed_segments=?, "
            "result_hash='', error_code='' WHERE goal_id='race'",
            (states.RUNNING, n_segments - 1),
        )

    def commit():
        barrier.wait()
        s.commit_segment(
            "race", n_segments - 1, seg, final_chain,
            crypto.input_fingerprint(inputs[-1]), 1.0,
        )

    def cancel():
        barrier.wait()
        s.request_cancel("race")

    for _ in range(100):
        reset_penultimate()
        t1 = threading.Thread(target=commit)
        t2 = threading.Thread(target=cancel)
        t1.start(); t2.start(); t1.join(); t2.join()
        g = s.get_goal("race")
        assert g.state in states.TERMINAL
        outcomes.add(g.state)
        # THE invariant: terminal state agrees with stored segment rows
        assert g.completed_segments == len(s.get_segments("race"))
        if g.state == states.SUCCEEDED:
            assert g.completed_segments == n_segments
            assert g.result_hash == final_chain
        else:
            assert g.state == states.CANCELED
            assert g.completed_segments == n_segments - 1
            assert g.result_hash == ""

    assert outcomes <= {states.SUCCEEDED, states.CANCELED}


def test_idempotent_segment_redelivery(store):
    fp = crypto.goal_fingerprint("g", ["a"], 100, 0)
    store.create_goal("g", ["a"], 100, 0, fp)
    store.mark_running("g")
    r, chained = _seg(store, "g", 0, ["a"])
    assert r.status == "committed"
    seg = crypto.segment_digest("g", 0, "a", 100)
    r2 = store.commit_segment(
        "g", 0, seg, chained, crypto.input_fingerprint("a"), 1.0
    )
    assert r2.status == "already_committed"
    assert len(store.get_segments("g")) == 1

    # conflicting redelivery raises
    with pytest.raises(RuntimeError):
        store.commit_segment(
            "g", 0, seg, "f" * 64, crypto.input_fingerprint("a"), 1.0
        )


def test_startup_sweep_and_recovery_flow(store):
    fp1 = crypto.goal_fingerprint("resume", ["a", "b"], 100,
                                  states.POLICY_RESUME)
    fp2 = crypto.goal_fingerprint("abort", ["a", "b"], 100,
                                  states.POLICY_ABORT)
    fp3 = crypto.goal_fingerprint("fresh", ["a"], 100, states.POLICY_RESUME)
    store.create_goal("resume", ["a", "b"], 100, states.POLICY_RESUME, fp1)
    store.create_goal("abort", ["a", "b"], 100, states.POLICY_ABORT, fp2)
    store.create_goal("fresh", ["a"], 100, states.POLICY_RESUME, fp3)
    store.mark_running("resume")
    store.mark_running("abort")
    # 'fresh' stays PENDING

    ids = store.mark_all_running_recoverable()
    assert set(ids) == {"resume", "abort", "fresh"}
    for gid in ids:
        assert store.get_goal(gid).state == states.RECOVERABLE

    changed, _ = store.recover_abort("abort")
    assert changed
    assert store.get_goal("abort").state == states.ABORTED
    assert store.get_goal("abort").error_code == states.ERR_ABORT_POLICY

    changed, _ = store.recover_begin("resume")
    assert changed and store.get_goal("resume").state == states.RUNNING
    assert store.mark_segments_recovered("resume") == 0

    # PENDING-at-crash goal resumes from segment 0
    changed, _ = store.recover_begin("fresh")
    assert changed


def test_reject_record_is_persisted(store):
    fp = crypto.goal_fingerprint("g", ["a"], 100, 0)
    store.create_goal("g", ["a"], 100, 0, fp)
    store.record_rejection("g", "deadbeef", states.ERR_CHANGED_PARAMS)
    rows = store.get_rejections("g")
    assert len(rows) == 1 and rows[0]["reason"] == states.ERR_CHANGED_PARAMS
    assert store.get_goal("g").last_rejection_code == states.ERR_CHANGED_PARAMS


def test_terminal_states_are_stable(store):
    fp = crypto.goal_fingerprint("g", ["a"], 100, 0)
    store.create_goal("g", ["a"], 100, 0, fp)
    store.mark_running("g")
    r, chained = _seg(store, "g", 0, ["a"])
    assert r.state == states.SUCCEEDED
    # cannot cancel a succeeded goal
    status, observed = store.request_cancel("g")
    assert status == "already_terminal" and observed == states.SUCCEEDED
    # cannot abort it either
    changed, observed = store.abort("g", "nope")
    assert not changed and observed == states.SUCCEEDED


def test_persistence_across_reopen(tmp_path):
    path = tmp_path / "persist.db"
    s1 = Storage(path)
    fp = crypto.goal_fingerprint("g", ["a", "b"], 100, 0)
    s1.create_goal("g", ["a", "b"], 100, 0, fp)
    s1.mark_running("g")
    _seg(s1, "g", 0, ["a", "b"])
    s1.close()

    s2 = Storage(path)
    g = s2.get_goal("g")
    assert g.state == states.RUNNING and g.completed_segments == 1
    assert len(s2.get_segments("g")) == 1
    s2.close()
