"""Unit tests for the SQLite consistency guarantees (no ROS required)."""

import threading

from segtask_server import status as st
from segtask_server.audit import verify_goal_chain
from segtask_server.db import Store


def _register(store, gid='g1', total=3, work=1000, cp=10, policy='resume'):
    row, created = store.register_goal(gid, total, work, cp, policy, 'phash')
    assert created
    return row


def test_register_is_idempotent_but_param_changes_visible(store):
    row, created = store.register_goal('a', 3, 10, 10, 'resume', 'h1')
    assert created and row['status'] == st.PENDING
    row2, created2 = store.register_goal('a', 3, 10, 10, 'resume', 'h1')
    assert not created2 and row2['goal_id'] == 'a'
    row3, created3 = store.register_goal('a', 9, 10, 10, 'resume', 'h2')
    assert not created3
    # Original params preserved - different params do not overwrite.
    assert row3['segments_total'] == 3 and row3['params_hash'] == 'h1'


def test_concurrent_duplicate_registration_single_winner(store):
    results = []

    def register():
        row, created = store.register_goal('race', 2, 5, 5, 'resume', 'h')
        results.append(created)

    threads = [threading.Thread(target=register) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and results.count(False) == 7


def test_full_success_path_and_events(store):
    _register(store)
    assert store.mark_running('g1')['status'] == st.RUNNING
    for i in range(3):
        ok, msg, row = store.commit_segment('g1', i, f'hash{i}')
        assert ok, msg
    final = store.finalize('g1')
    assert final['status'] == st.SUCCEEDED
    assert final['segments_done'] == 3
    assert final['result_hash'] == 'hash2'
    types = [e['event_type'] for e in store.events('g1')]
    assert types == ['GOAL_ACCEPTED', 'GOAL_STARTED', 'SEGMENT_COMMITTED'] * 0 + [
        'GOAL_ACCEPTED', 'GOAL_STARTED',
        'SEGMENT_COMMITTED', 'SEGMENT_COMMITTED', 'SEGMENT_COMMITTED',
        'GOAL_SUCCEEDED']


def test_out_of_order_segment_commit_rejected(store):
    _register(store)
    store.mark_running('g1')
    ok, msg, _ = store.commit_segment('g1', 1, 'x')
    assert not ok and 'expected segment index 0' in msg
    ok, _, _ = store.commit_segment('g1', 0, 'h0')
    assert ok
    ok, msg, _ = store.commit_segment('g1', 0, 'dup')
    assert not ok  # primary key collision / index mismatch


def test_canceled_goal_cannot_commit_more_segments(store):
    _register(store, total=4)
    store.mark_running('g1')
    assert store.commit_segment('g1', 0, 'h0')[0]
    ok, msg = store.request_cancel('g1')
    assert ok
    # Worker is mid-flight and tries to land its computed result: rejected.
    ok, msg, _ = store.commit_segment('g1', 1, 'h1')
    assert not ok and 'CANCELED' in msg.upper() or 'CANCELING' in msg
    final = store.finalize('g1')
    assert final['status'] == st.CANCELED and final['segments_done'] == 1
    # After terminal state, still rejected.
    ok, _, _ = store.commit_segment('g1', 1, 'h1-late')
    assert not ok


def test_cancel_after_last_commit_produces_single_terminal_state(store):
    """The cancel/complete race: cancel recorded AFTER the last commit.

    Both workers call finalize(); the DB CAS guarantees one terminal row and
    the final outcome is CANCELED (cancel request wins over completion per
    the documented arbitration policy).
    """
    _register(store, total=2)
    store.mark_running('g1')
    store.commit_segment('g1', 0, 'h0')
    store.commit_segment('g1', 1, 'h1')
    # Cancel arrives in the window between last commit and finalize.
    ok, _ = store.request_cancel('g1')
    assert ok
    f1 = store.finalize('g1')
    assert f1['status'] == st.CANCELED and f1['segments_done'] == 2
    # Completion path "wins nothing" on the second call.
    f2 = store.finalize('g1')
    assert f2['status'] == st.CANCELED
    types = [e['event_type'] for e in store.events('g1')]
    assert types.count('GOAL_CANCELED') == 1
    assert 'GOAL_SUCCEEDED' not in types


def test_complete_wins_when_no_cancel(store):
    _register(store, total=1)
    store.mark_running('g1')
    store.commit_segment('g1', 0, 'h0')
    final = store.finalize('g1')
    assert final['status'] == st.SUCCEEDED


def test_concurrent_finalize_race_one_terminal(store):
    _register(store, total=2)
    store.mark_running('g1')
    store.commit_segment('g1', 0, 'h0')
    store.commit_segment('g1', 1, 'h1')
    outcomes = []

    def cancel_then_finalize():
        store.request_cancel('g1')
        outcomes.append(store.finalize('g1')['status'])

    def just_finalize():
        outcomes.append(store.finalize('g1')['status'])

    t1 = threading.Thread(target=cancel_then_finalize)
    t2 = threading.Thread(target=just_finalize)
    t1.start(); t2.start()
    t1.join(); t2.join()
    assert set(outcomes) == {st.CANCELED}  # both read the single final row


def test_cancel_terminal_goal_rejected_and_idempotent(store):
    _register(store)
    store.mark_running('g1')
    assert store.request_cancel('g1')[0]
    assert store.request_cancel('g1') == (True, 'cancel already requested')
    store.finalize('g1')
    ok, msg = store.request_cancel('g1')
    assert not ok and 'terminal' in msg


def test_startup_recovery_resume_and_abort_policies(tmp_path, secret):
    db = str(tmp_path / 'r.db')
    s1 = Store(db, secret, run_id='run-before-crash')
    s1.register_goal('resume-g', 3, 5, 5, 'resume', 'h')
    s1.mark_running('resume-g')
    s1.commit_segment('resume-g', 0, 'h0')
    s1.register_goal('abort-g', 3, 5, 5, 'abort', 'h')
    s1.mark_running('abort-g')
    s1.append_server_event('SIMULATED_CRASH', {})
    s1.close()

    s2 = Store(db, secret, run_id='run-after-restart')
    recovered = s2.startup_recovery('resume')
    resume_row = s2.get_goal('resume-g')
    abort_row = s2.get_goal('abort-g')
    assert resume_row['status'] == st.RECOVERING
    assert resume_row['resumed_after_restart'] == 1
    assert resume_row['segments_done'] == 1  # progress preserved
    assert abort_row['status'] == st.ABORTED  # per-goal abort policy applied
    assert len(recovered) == 1 and recovered[0]['goal_id'] == 'resume-g'

    # Recovery can continue from committed progress.
    assert s2.commit_segment('resume-g', 1, 'h1')[0]
    assert s2.commit_segment('resume-g', 2, 'h2')[0]
    final = s2.finalize('resume-g')
    assert final['status'] == st.SUCCEEDED
    assert final['segments_done'] == 3
    s2.close()


def test_startup_recovery_manual_mode_parks_goals(tmp_path, secret):
    db = str(tmp_path / 'm.db')
    s1 = Store(db, secret, run_id='r1')
    s1.register_goal('mg', 2, 5, 5, 'resume', 'h')
    s1.mark_running('mg')
    s1.close()
    s2 = Store(db, secret, run_id='r2')
    recovered = s2.startup_recovery('manual')
    assert recovered == []
    row = s2.get_goal('mg')
    assert row['status'] == st.RECOVERING
    # Admin abort works on parked goals.
    ok, msg = s2.admin_abort('mg')
    assert ok
    assert s2.get_goal('mg')['status'] == st.ABORTED
    s2.close()


def test_cancel_requested_survives_restart(tmp_path, secret):
    db = str(tmp_path / 'c.db')
    s1 = Store(db, secret, run_id='r1')
    s1.register_goal('cg', 3, 5, 5, 'resume', 'h')
    s1.mark_running('cg')
    s1.commit_segment('cg', 0, 'h0')
    s1.request_cancel('cg')
    s1.close()
    s2 = Store(db, secret, run_id='r2')
    s2.startup_recovery('resume')
    row = s2.get_goal('cg')
    # Recovery parks it RECOVERING, but sticky cancel remains; finalize cancels.
    assert row['cancel_requested'] == 1
    final = s2.finalize('cg')
    assert final['status'] == st.CANCELED and final['segments_done'] == 1
    s2.close()


def test_chain_verifies_and_detects_tampering(store):
    _register(store, 'g1', 2)
    store.mark_running('g1')
    store.commit_segment('g1', 0, 'h0')
    store.finalize('g1')
    ok, err = verify_goal_chain(store, 'g1')
    assert ok, err

    # Tamper directly with SQLite: change a segment count.
    with store._lock:
        store._conn.execute(
            "UPDATE goals SET segments_done=99 WHERE goal_id='g1'")
        store._conn.commit()
    # Segment-event vs goal count mismatch is visible via raw rows; more
    # importantly, tamper with an event payload and detect the broken HMAC.
    with store._lock:
        store._conn.execute(
            "UPDATE goal_events SET payload_json=REPLACE(payload_json,'\"segment_index\":0',"
            "'\"segment_index\":9') WHERE event_type='SEGMENT_COMMITTED'")
        store._conn.commit()
    ok, err = verify_goal_chain(store, 'g1')
    assert not ok and 'HMAC mismatch' in err


def test_delete_event_breaks_chain(store):
    _register(store, 'g1', 1)
    store.mark_running('g1')
    store.commit_segment('g1', 0, 'h0')
    store.finalize('g1')
    with store._lock:
        store._conn.execute('DELETE FROM goal_events WHERE event_type="GOAL_STARTED"')
        store._conn.commit()
    ok, err = verify_goal_chain(store, 'g1')
    assert not ok


def test_admin_abort_rejected_for_terminal(store):
    _register(store)
    store.mark_running('g1')
    store.request_cancel('g1')
    store.finalize('g1')
    ok, _ = store.admin_abort('g1')
    assert not ok
