//! Integration tests for the lock manager.
//!
//! Covers the acceptance scenarios:
//! * two-transaction (2-edge) deadlock cycle
//! * three-transaction (3-edge) deadlock cycle
//! * adjacent / non-overlapping intervals never block (no false positives)
//! * shared locks and S->X lock upgrade
//! * victim releases ALL locks and the others proceed
//! * FIFO queue behaviour and explicit abort/commit release paths

use std::time::Duration;

use interval_lock::{Interval, LockError, LockManager, LockOutcome, Mode, TxnState};

fn iv(start: i64, end: i64) -> Interval {
    Interval::new(start, end).unwrap()
}

fn is_granted(r: &Result<LockOutcome, LockError>) -> bool {
    matches!(r, Ok(LockOutcome::Granted))
}

// ---------- basics ----------

#[test]
fn shared_locks_are_compatible() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Shared, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Shared, iv(0, 10))));
    assert!(lm.commit(a).is_ok());
    assert!(lm.commit(b).is_ok());
}

#[test]
fn exclusive_blocks_shared_and_vice_versa() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    match lm.lock(b, Mode::Shared, iv(0, 10)) {
        Ok(LockOutcome::Waiting { waiting_for }) => assert_eq!(waiting_for, vec![a]),
        other => panic!("expected waiting, got {:?}", other),
    }
    // X while S held also blocks.
    let d = lm.begin_txn();
    let e = lm.begin_txn();
    assert!(is_granted(&lm.lock(d, Mode::Shared, iv(100, 110))));
    match lm.lock(e, Mode::Exclusive, iv(100, 110)) {
        Ok(LockOutcome::Waiting { waiting_for }) => assert_eq!(waiting_for, vec![d]),
        other => panic!("expected waiting, got {:?}", other),
    }
}

#[test]
fn adjacent_intervals_do_not_conflict() {
    let lm = LockManager::new();
    // Half-open: [0,10) and [10,20) touch but never overlap.
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(10, 20))));
}

#[test]
fn non_overlapping_intervals_do_not_conflict() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(100, 110))));
    // Same key point in reverse order too.
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(-5, 0))));
}

#[test]
fn invalid_intervals_are_rejected() {
    assert_eq!(Interval::new(5, 5), Err(LockError::InvalidInterval));
    assert_eq!(Interval::new(6, 5), Err(LockError::InvalidInterval));
}

// ---------- lock upgrade ----------

#[test]
fn upgrade_s_to_x_without_other_shared_holders() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Shared, iv(0, 10))));
    // Self does not block self: upgrade S -> X on the same interval.
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    let s = lm.state();
    let t = s.txns.iter().find(|t| t.id == a).unwrap();
    assert_eq!(t.held.len(), 2);
}

#[test]
fn upgrade_blocks_when_another_shared_holder_exists() {
    // A holds S, B holds S. A upgrades to X -> blocked by B.
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Shared, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Shared, iv(0, 10))));
    match lm.lock(a, Mode::Exclusive, iv(0, 10)) {
        Ok(LockOutcome::Waiting { waiting_for }) => assert_eq!(waiting_for, vec![b]),
        other => panic!("expected waiting on b, got {:?}", other),
    }
    // B commits -> A's upgrade goes through (FIFO processing).
    assert!(lm.commit(b).is_ok());
    let s = lm.state();
    let ta = s.txns.iter().find(|t| t.id == a).unwrap();
    assert!(ta.waiting.is_none());
    assert!(ta.held.iter().any(|h| h.mode == Mode::Exclusive));
}

// ---------- deadlock: two transactions ----------

#[test]
fn two_txn_deadlock_picks_max_id_as_victim() {
    let lm = LockManager::new();
    let a = lm.begin_txn(); // T1
    let b = lm.begin_txn(); // T2
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(20, 30))));

    // b waits for a
    assert!(matches!(
        lm.lock(b, Mode::Exclusive, iv(0, 10)),
        Ok(LockOutcome::Waiting { .. })
    ));
    // a waits for b -> cycle 1->2->1; victim must be the larger id (b).
    match lm.lock(a, Mode::Exclusive, iv(20, 30)) {
        Ok(LockOutcome::Deadlock { victim, cycle }) => {
            assert_eq!(victim, b);
            assert_eq!(cycle.first(), cycle.last());
            assert!(cycle.contains(&a) && cycle.contains(&b));
        }
        other => panic!("expected deadlock, got {:?}", other),
    }

    // Victim b: aborted, released ALL its locks (both the [20,30) X and... it
    // held only [20,30); verify nothing remains and it is aborted).
    let s = lm.state();
    let tb = s.txns.iter().find(|t| t.id == b).unwrap();
    assert_eq!(tb.state, TxnState::Aborted);
    assert!(tb.held.is_empty());

    // Survivor a must continue: its last request [20,30) was granted when the
    // victim released the lock.
    let ta = s.txns.iter().find(|t| t.id == a).unwrap();
    assert_eq!(ta.state, TxnState::Active);
    assert!(ta.waiting.is_none());
    assert!(ta.held.iter().any(|h| h.interval == iv(20, 30)));
    // No wait edges remain.
    assert!(s.wait_for.is_empty());

    // b cannot do anything now.
    assert!(matches!(
        lm.lock(b, Mode::Exclusive, iv(0, 1)),
        Err(LockError::TxnNotActive { state: TxnState::Aborted })
    ));
    // a finishes cleanly.
    assert!(lm.commit(a).is_ok());
}

// ---------- deadlock: three transactions ----------

#[test]
fn three_txn_deadlock_victim_is_max_in_cycle() {
    let lm = LockManager::new();
    let a = lm.begin_txn(); // T1
    let b = lm.begin_txn(); // T2
    let c = lm.begin_txn(); // T3

    // a: X[0,10), b: X[10,20), c: X[20,30)
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(10, 20))));
    assert!(is_granted(&lm.lock(c, Mode::Exclusive, iv(20, 30))));

    // build a->c, c->b, b->a (cycle 1->3->2->1)
    assert!(matches!(
        lm.lock(a, Mode::Exclusive, iv(20, 30)), // a waits for c
        Ok(LockOutcome::Waiting { .. })
    ));
    assert!(matches!(
        lm.lock(c, Mode::Exclusive, iv(10, 20)), // c waits for b
        Ok(LockOutcome::Waiting { .. })
    ));
    match lm.lock(b, Mode::Exclusive, iv(0, 10)) {
        // b waits for a -> closes the cycle; victim = max id = c (T3)
        Ok(LockOutcome::Deadlock { victim, cycle }) => {
            assert_eq!(victim, c);
            assert_eq!(cycle.len(), 4, "cycle should list 3 nodes plus repeat: {:?}", cycle);
            assert!(cycle.contains(&a) && cycle.contains(&b) && cycle.contains(&c));
        }
        other => panic!("expected deadlock, got {:?}", other),
    }

    let s = lm.state();
    // c aborted and released [20,30); a's queued request for [20,30) granted.
    let tc = s.txns.iter().find(|t| t.id == c).unwrap();
    assert_eq!(tc.state, TxnState::Aborted);
    assert!(tc.held.is_empty());
    let ta = s.txns.iter().find(|t| t.id == a).unwrap();
    assert!(ta.waiting.is_none());
    assert!(ta.held.iter().any(|h| h.interval == iv(20, 30)));
    // b's request [0,10) is still blocked by a's original [0,10) lock.
    let tb = s.txns.iter().find(|t| t.id == b).unwrap();
    assert_eq!(tb.state, TxnState::Active);
    assert!(tb.waiting.is_some());
    // once a commits, b proceeds
    assert!(lm.commit(a).is_ok());
    let s = lm.state();
    let tb = s.txns.iter().find(|t| t.id == b).unwrap();
    assert!(tb.waiting.is_none());
    assert!(tb.held.iter().any(|h| h.interval == iv(0, 10)));
    assert!(lm.commit(b).is_ok());
}

// ---------- no false positives ----------

#[test]
fn no_deadlock_without_a_cycle() {
    // Chain b->a, c->b but a never waits: no cycle, nobody may be aborted.
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    let c = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(10, 20))));

    assert!(matches!(
        lm.lock(b, Mode::Exclusive, iv(0, 10)),
        Ok(LockOutcome::Waiting { .. })
    ));
    assert!(matches!(
        lm.lock(c, Mode::Exclusive, iv(10, 20)),
        Ok(LockOutcome::Waiting { .. })
    ));
    let s = lm.state();
    assert_eq!(s.txns.iter().filter(|t| t.state == TxnState::Aborted).count(), 0);
    assert_eq!(s.wait_for.len(), 2);
    // release chain: a commits -> b gets [0,10) -> c gets [10,20)
    assert!(lm.commit(a).is_ok());
    assert!(lm.commit(b).is_ok());
    let s = lm.state();
    let tc = s.txns.iter().find(|t| t.id == c).unwrap();
    assert!(tc.waiting.is_none());
    assert!(tc.held.iter().any(|h| h.interval == iv(10, 20)));
}

#[test]
fn adjacent_intervals_never_create_edges() {
    // A/B hold locks on adjacent intervals; cross-requests on the *other*
    // adjacent interval must all be granted, and the wait-for graph stay empty.
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(10, 20))));
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(20, 30))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(-10, 0))));
    let s = lm.state();
    assert!(s.wait_for.is_empty());
    assert!(s.queue.is_empty());
}

// ---------- abort releases all locks ----------

#[test]
fn explicit_abort_releases_all_locks_and_unblocks() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    let c = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(100, 110))));
    assert!(matches!(
        lm.lock(b, Mode::Exclusive, iv(0, 10)),
        Ok(LockOutcome::Waiting { .. })
    ));
    assert!(matches!(
        lm.lock(c, Mode::Exclusive, iv(100, 110)),
        Ok(LockOutcome::Waiting { .. })
    ));
    assert!(lm.abort(a).is_ok());
    let s = lm.state();
    let ta = s.txns.iter().find(|t| t.id == a).unwrap();
    assert!(ta.held.is_empty());
    assert!(s.wait_for.is_empty());
    for id in [b, c] {
        let t = s.txns.iter().find(|t| t.id == id).unwrap();
        assert!(t.waiting.is_none());
    }
    assert!(lm.commit(b).is_ok());
    assert!(lm.commit(c).is_ok());
}

// ---------- queue fairness ----------

#[test]
fn fifo_queue_blocks_queue_jumpers() {
    // a holds X[0,20). b queues X[0,10); c queues S[5,15) (overlaps b's wait).
    // c is compatible with... nothing: a's held X blocks it, and strict FIFO
    // also makes c wait behind b's earlier conflicting X — no queue jumping.
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    let c = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 20))));
    assert!(matches!(
        lm.lock(b, Mode::Exclusive, iv(0, 10)),
        Ok(LockOutcome::Waiting { .. })
    ));
    match lm.lock(c, Mode::Shared, iv(5, 15)) {
        Ok(LockOutcome::Waiting { waiting_for }) => {
            // blocked by a (holder) and b (earlier queue entry)
            assert!(waiting_for.contains(&a) && waiting_for.contains(&b));
        }
        other => panic!("expected waiting, got {:?}", other),
    }
    assert!(lm.commit(a).is_ok());
    // b grants first; c still cannot jump past b's now-held X[0,10).
    let s = lm.state();
    let tb = s.txns.iter().find(|t| t.id == b).unwrap();
    let tc = s.txns.iter().find(|t| t.id == c).unwrap();
    assert!(tb.waiting.is_none());
    assert!(tc.waiting.is_some());
    // once b commits too, c is granted.
    assert!(lm.commit(b).is_ok());
    let s = lm.state();
    let tc = s.txns.iter().find(|t| t.id == c).unwrap();
    assert!(tc.waiting.is_none());
    assert!(tc.held.iter().any(|h| h.mode == Mode::Shared));
}

// ---------- blocking API ----------

#[tokio::test]
async fn blocking_lock_grants_after_release() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));

    let lm2 = lm.clone();
    let waiter = tokio::spawn(async move {
        lm2.lock_blocking(b, Mode::Exclusive, iv(0, 10), Duration::from_secs(5))
            .await
    });
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(lm.commit(a).is_ok());
    let r = tokio::time::timeout(Duration::from_secs(2), waiter)
        .await
        .expect("waiter hung")
        .expect("join error")
        .expect("blocking lock should succeed after commit");
    assert_eq!(r, LockOutcome::Granted);
}

#[tokio::test]
async fn blocking_lock_reports_deadlock_abort() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    assert!(is_granted(&lm.lock(b, Mode::Exclusive, iv(20, 30))));

    let lm2 = lm.clone();
    // b blocks waiting for a's lock.
    let waiter = tokio::spawn(async move {
        lm2.lock_blocking(b, Mode::Exclusive, iv(0, 10), Duration::from_secs(10))
            .await
    });
    tokio::time::sleep(Duration::from_millis(100)).await;

    // a closes the cycle; the deterministic victim is b (larger id), which
    // aborts the blocking call with LockError::Aborted.
    match lm.lock(a, Mode::Exclusive, iv(20, 30)) {
        Ok(LockOutcome::Deadlock { victim, .. }) => assert_eq!(victim, b),
        other => panic!("expected deadlock, got {:?}", other),
    }
    let r = tokio::time::timeout(Duration::from_secs(2), waiter)
        .await
        .expect("waiter hung")
        .expect("join error");
    assert!(matches!(r, Err(LockError::Aborted)));
    // a survived and continues.
    assert!(lm.commit(a).is_ok());
}

#[tokio::test]
async fn blocking_lock_times_out_and_leaves_no_orphan() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 10))));
    let r = lm
        .lock_blocking(b, Mode::Exclusive, iv(0, 10), Duration::from_millis(100))
        .await;
    assert!(matches!(r, Err(LockError::Timeout)));
    let s = lm.state();
    assert!(s.queue.is_empty(), "timed-out request must leave the queue");
    // A subsequent request by b is accepted (no stuck AlreadyWaiting).
    assert!(lm.lock(b, Mode::Exclusive, iv(100, 110)).is_ok());
}

// ---------- lifecycle ----------

#[test]
fn unknown_and_finished_txns_are_rejected() {
    let lm = LockManager::new();
    assert!(matches!(lm.lock(99, Mode::Shared, iv(0, 1)), Err(LockError::TxnNotFound)));
    assert!(matches!(lm.commit(99), Err(LockError::TxnNotFound)));
    assert!(matches!(lm.abort(99), Err(LockError::TxnNotFound)));

    let a = lm.begin_txn();
    assert!(lm.commit(a).is_ok());
    assert!(matches!(
        lm.lock(a, Mode::Shared, iv(0, 1)),
        Err(LockError::TxnNotActive { state: TxnState::Committed })
    ));
}

#[test]
fn one_waiting_request_per_txn_is_enforced() {
    let lm = LockManager::new();
    let a = lm.begin_txn();
    let b = lm.begin_txn();
    assert!(is_granted(&lm.lock(a, Mode::Exclusive, iv(0, 40))));
    assert!(matches!(
        lm.lock(b, Mode::Exclusive, iv(0, 10)),
        Ok(LockOutcome::Waiting { .. })
    ));
    assert!(matches!(lm.lock(b, Mode::Exclusive, iv(10, 20)), Err(LockError::AlreadyWaiting)));
}
