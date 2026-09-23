//! Core interval-lock manager.
//!
//! Design notes
//! ------------
//! * Intervals are half-open `[start, end)` over `i64`.
//! * Two lock modes: `Shared` (S) and `Exclusive` (X). S is compatible with S,
//!   everything else conflicts (when intervals overlap).
//! * One FIFO wait queue. A request is blocked when it conflicts with a granted
//!   lock of another transaction, or with an *earlier* queued request of another
//!   transaction (FIFO fairness, no queue jumping).
//! * The wait-for graph (WFG) is derived from the *actual* blockers of each
//!   waiting request, so every edge reflects real blocking and cycles are real
//!   deadlocks (no false positives).
//! * Deadlock detection runs when a request is enqueued. The victim is chosen
//!   deterministically: the transaction with the numerically largest id in the
//!   cycle. The victim is aborted and releases *all* of its locks, after which
//!   the queue is re-processed so other transactions can proceed.
//! * A transaction may hold many locks but has at most one outstanding
//!   (waiting) request at a time.

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::sync::Notify;

/// Lock mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    Shared,
    Exclusive,
}

impl Mode {
    /// Classic S/X compatibility matrix.
    fn compatible(self, other: Mode) -> bool {
        matches!((self, other), (Mode::Shared, Mode::Shared))
    }
}

/// Half-open interval `[start, end)`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Interval {
    pub start: i64,
    pub end: i64,
}

impl Interval {
    pub fn new(start: i64, end: i64) -> Result<Interval, LockError> {
        if start >= end {
            return Err(LockError::InvalidInterval);
        }
        Ok(Interval { start, end })
    }

    /// Half-open overlap: `[a,b)` and `[c,d)` overlap iff `a < d && c < b`.
    /// Adjacent intervals (`b == c`) do NOT overlap.
    pub fn overlaps(&self, other: &Interval) -> bool {
        self.start < other.end && other.start < self.end
    }

    /// `self` fully covers `other`.
    pub fn covers(&self, other: &Interval) -> bool {
        self.start <= other.start && other.end <= self.end
    }
}

/// A single lock request (granted or waiting).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct LockRequest {
    pub txn: u64,
    pub mode: Mode,
    pub interval: Interval,
}

/// Lifecycle state of a transaction.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TxnState {
    Active,
    Committed,
    Aborted,
}

struct Txn {
    id: u64,
    state: TxnState,
    /// All locks currently granted to this transaction.
    held: Vec<LockRequest>,
    notify: Arc<Notify>,
}

/// Errors returned by the lock manager (mapped to HTTP status codes in `main`).
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "error", rename_all = "snake_case")]
pub enum LockError {
    InvalidInterval,
    TxnNotFound,
    TxnNotActive { state: TxnState },
    LockNotHeld,
    AlreadyWaiting,
    /// A blocking lock request did not complete before its timeout.
    Timeout,
    /// The transaction was aborted (as a deadlock victim or explicitly).
    Aborted,
}

/// Outcome of a lock request.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status", rename_all = "lowercase")]
pub enum LockOutcome {
    /// The lock was granted immediately.
    Granted,
    /// The request is queued; the transaction is now blocked on `waiting_for`.
    Waiting { waiting_for: Vec<u64> },
    /// A deadlock was detected; `victim` was aborted (releases all its locks).
    /// `Some(txn)` in `granted` means this request itself was granted right
    /// after the victim released its locks.
    Deadlock { victim: u64, cycle: Vec<u64> },
}

/// Why one transaction waits for another.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum WaitReason {
    /// The other transaction holds a conflicting granted lock.
    Holder,
    /// The other transaction has an earlier conflicting request in the queue.
    Queue,
}

/// One edge of the wait-for graph.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WaitEdge {
    pub from: u64,
    pub to: u64,
    pub reason: WaitReason,
}

/// Serializable view of a transaction (for `/state`).
#[derive(Debug, Clone, Serialize)]
pub struct TxnView {
    pub id: u64,
    pub state: TxnState,
    pub held: Vec<LockRequest>,
    pub waiting: Option<LockRequest>,
    pub waiting_for: Vec<u64>,
}

/// Serializable snapshot of the whole manager (for `/state`).
#[derive(Debug, Clone, Serialize)]
pub struct StateView {
    pub txns: Vec<TxnView>,
    pub queue: Vec<LockRequest>,
    pub wait_for: Vec<WaitEdge>,
    /// Recent events (bounded, oldest first).
    pub log: Vec<String>,
}

struct Core {
    txns: BTreeMap<u64, Txn>,
    /// FIFO queue of waiting requests. Invariant: at most one entry per txn.
    queue: Vec<LockRequest>,
    next_txn: u64,
    log: Vec<String>,
}

const MAX_LOG: usize = 500;

impl Core {
    fn log(&mut self, msg: String) {
        if self.log.len() >= MAX_LOG {
            self.log.remove(0);
        }
        self.log.push(msg);
    }

    fn txn(&self, id: u64) -> Result<&Txn, LockError> {
        self.txns.get(&id).ok_or(LockError::TxnNotFound)
    }

    fn txn_active(&self, id: u64) -> Result<&Txn, LockError> {
        let t = self.txn(id)?;
        if t.state != TxnState::Active {
            return Err(LockError::TxnNotActive { state: t.state });
        }
        Ok(t)
    }

    fn waiting_request(&self, txn: u64) -> Option<&LockRequest> {
        self.queue.iter().find(|r| r.txn == txn)
    }

    /// Transactions that currently block `req`, considering all granted locks
    /// and all queued requests *strictly before* `before_pos` in the queue.
    /// Locks/requests of `req.txn` itself are ignored (self never blocks self,
    /// which is also what makes lock upgrade possible).
    fn blockers(&self, req: &LockRequest, before_pos: usize) -> BTreeSet<u64> {
        let mut out = BTreeSet::new();
        for t in self.txns.values() {
            if t.id == req.txn || t.state != TxnState::Active {
                continue;
            }
            for h in &t.held {
                if h.interval.overlaps(&req.interval) && !h.mode.compatible(req.mode) {
                    out.insert(t.id);
                    break;
                }
            }
        }
        for q in self.queue.iter().take(before_pos) {
            if q.txn == req.txn {
                continue;
            }
            if q.interval.overlaps(&req.interval) && !q.mode.compatible(req.mode) {
                out.insert(q.txn);
            }
        }
        out
    }

    /// Can `req` be granted right now? (no blockers anywhere in the system)
    fn can_grant(&self, req: &LockRequest) -> bool {
        self.blockers(req, self.queue.len()).is_empty()
    }

    /// Grant `req` to its transaction (idempotent for identical re-requests).
    fn grant(&mut self, req: LockRequest) {
        let t = self.txns.get_mut(&req.txn).expect("grantee must exist");
        if !t.held.contains(&req) {
            t.held.push(req);
        }
        self.log(format!(
            "granted {:?} [{},{}) to T{}",
            req.mode, req.interval.start, req.interval.end, req.txn
        ));
    }

    /// Remove every queued request of `txn` (there is at most one).
    fn remove_from_queue(&mut self, txn: u64) {
        self.queue.retain(|r| r.txn != txn);
    }

    /// Abort `txn`: mark it aborted, drop all its locks and its queued request,
    /// wake up any blocked caller, then re-process the queue.
    /// Returns the locks that were released.
    fn abort_inner(&mut self, txn: u64, cause: &str) -> Vec<LockRequest> {
        let (released, notify) = {
            let t = match self.txns.get_mut(&txn) {
                Some(t) => t,
                None => return Vec::new(),
            };
            if t.state != TxnState::Active {
                return Vec::new();
            }
            t.state = TxnState::Aborted;
            let released = std::mem::take(&mut t.held);
            (released, t.notify.clone())
        };
        self.remove_from_queue(txn);
        self.log(format!("aborted T{} ({}), released {} lock(s)", txn, cause, released.len()));
        notify.notify_waiters();
        self.process_queue();
        released
    }

    /// Single FIFO pass over the queue: grant every request that is no longer
    /// blocked. Blocked requests stay in place (strict FIFO, no jumping).
    fn process_queue(&mut self) {
        let mut i = 0;
        while i < self.queue.len() {
            let req = self.queue[i];
            let blocked = !self.blockers(&req, i).is_empty();
            if blocked {
                i += 1;
                continue;
            }
            // Only grant if the transaction is still active (it may have been
            // aborted while waiting).
            let active = self
                .txns
                .get(&req.txn)
                .map(|t| t.state == TxnState::Active)
                .unwrap_or(false);
            let notify = self.txns.get(&req.txn).map(|t| t.notify.clone());
            self.queue.remove(i);
            if active {
                self.grant(req);
            }
            if let Some(n) = notify {
                n.notify_waiters();
            }
            // do not advance i: the next request shifted into position i
        }
    }

    /// Current wait-for edges for every waiting transaction.
    fn wait_for_edges(&self) -> Vec<WaitEdge> {
        let mut edges = Vec::new();
        for (i, req) in self.queue.iter().enumerate() {
            let blockers = self.blockers(req, i);
            for b in blockers {
                // Holder edge if that txn holds a conflicting granted lock,
                // otherwise it blocks us via an earlier queue entry.
                let reason = if self
                    .txns
                    .get(&b)
                    .map(|t| {
                        t.held.iter().any(|h| {
                            h.interval.overlaps(&req.interval) && !h.mode.compatible(req.mode)
                        })
                    })
                    .unwrap_or(false)
                {
                    WaitReason::Holder
                } else {
                    WaitReason::Queue
                };
                edges.push(WaitEdge { from: req.txn, to: b, reason });
            }
        }
        edges
    }

    /// Find a cycle reachable from `start` in the wait-for graph.
    /// Deterministic: neighbours are visited in ascending txn-id order.
    /// Returns the cycle as a list of txn ids (first == would-be-last).
    fn find_cycle_from(&self, start: u64) -> Option<Vec<u64>> {
        // adjacency: from -> neighbours in ascending order (deterministic DFS)
        let mut adj: BTreeMap<u64, Vec<u64>> = BTreeMap::new();
        for e in self.wait_for_edges() {
            adj.entry(e.from).or_default().push(e.to);
        }
        for v in adj.values_mut() {
            v.sort_unstable();
            v.dedup();
        }

        // Iterative DFS from `start`, looking for an edge back to `start`.
        let mut path: Vec<u64> = vec![start];
        let mut on_path: BTreeSet<u64> = BTreeSet::from([start]);
        let mut idx_stack: Vec<usize> = vec![0];
        while !path.is_empty() {
            let cur = *path.last().unwrap();
            let i = *idx_stack.last().unwrap();
            let neighbours = adj.get(&cur).cloned().unwrap_or_default();
            if i < neighbours.len() {
                *idx_stack.last_mut().unwrap() = i + 1;
                let next = neighbours[i];
                if next == start {
                    // found a cycle back to start
                    let mut cyc = path.clone();
                    cyc.push(start);
                    return Some(cyc);
                }
                if !on_path.contains(&next) {
                    on_path.insert(next);
                    path.push(next);
                    idx_stack.push(0);
                }
                // nodes already on the path are skipped: cycles not involving
                // `start` cannot exist (detection runs on every enqueue)
            } else {
                path.pop();
                idx_stack.pop();
                on_path.remove(&cur);
            }
        }
        None
    }
}

/// The public lock manager handle. Cheap to clone, safe to share.
#[derive(Clone)]
pub struct LockManager {
    core: Arc<Mutex<Core>>,
}

impl Default for LockManager {
    fn default() -> Self {
        Self::new()
    }
}

impl LockManager {
    pub fn new() -> Self {
        LockManager {
            core: Arc::new(Mutex::new(Core {
                txns: BTreeMap::new(),
                queue: Vec::new(),
                next_txn: 1,
                log: Vec::new(),
            })),
        }
    }

    fn guard(&self) -> std::sync::MutexGuard<'_, Core> {
        self.core.lock().expect("lock manager mutex poisoned")
    }

    /// Begin a new transaction. Returns its id (monotonically increasing,
    /// starting at 1). Ids are also used for deterministic victim selection:
    /// in a deadlock cycle the *largest* id is aborted.
    pub fn begin_txn(&self) -> u64 {
        let mut c = self.guard();
        let id = c.next_txn;
        c.next_txn += 1;
        c.txns.insert(
            id,
            Txn {
                id,
                state: TxnState::Active,
                held: Vec::new(),
                notify: Arc::new(Notify::new()),
            },
        );
        c.log(format!("begin T{}", id));
        id
    }

    /// Commit: release all locks, drop any queued request, wake waiters.
    pub fn commit(&self, txn: u64) -> Result<TxnState, LockError> {
        let mut c = self.guard();
        let t = c.txn(txn)?;
        match t.state {
            TxnState::Committed => return Ok(TxnState::Committed), // idempotent
            TxnState::Aborted => return Err(LockError::TxnNotActive { state: TxnState::Aborted }),
            TxnState::Active => {}
        }
        let notify = t.notify.clone();
        c.txns.get_mut(&txn).unwrap().state = TxnState::Committed;
        c.txns.get_mut(&txn).unwrap().held.clear();
        c.remove_from_queue(txn);
        c.log(format!("committed T{}", txn));
        notify.notify_waiters();
        c.process_queue();
        Ok(TxnState::Committed)
    }

    /// Abort: release all locks, drop any queued request, wake waiters.
    /// Idempotent: aborting an already-aborted txn is a no-op returning its state.
    pub fn abort(&self, txn: u64) -> Result<TxnState, LockError> {
        let mut c = self.guard();
        let t = c.txn(txn)?;
        match t.state {
            TxnState::Aborted => return Ok(TxnState::Aborted),
            TxnState::Committed => {
                return Err(LockError::TxnNotActive { state: TxnState::Committed })
            }
            TxnState::Active => {}
        }
        c.abort_inner(txn, "explicit abort");
        Ok(TxnState::Aborted)
    }

    /// Non-blocking lock request.
    pub fn lock(&self, txn: u64, mode: Mode, interval: Interval) -> Result<LockOutcome, LockError> {
        let mut c = self.guard();
        self.lock_inner(&mut c, txn, mode, interval)
    }

    fn lock_inner(
        &self,
        c: &mut Core,
        txn: u64,
        mode: Mode,
        interval: Interval,
    ) -> Result<LockOutcome, LockError> {
        c.txn_active(txn)?;
        let req = LockRequest { txn, mode, interval };
        if let Some(existing) = c.waiting_request(txn) {
            // Idempotent re-post of the *same* request: report current status
            // instead of erroring. A second, different request while waiting
            // is still rejected (one outstanding request per transaction).
            if *existing == req {
                let pos = c.queue.iter().position(|q| *q == req).unwrap();
                let waiting_for: Vec<u64> = c.blockers(&req, pos).into_iter().collect();
                return Ok(LockOutcome::Waiting { waiting_for });
            }
            return Err(LockError::AlreadyWaiting);
        }

        // Idempotent: an identical lock already held -> granted.
        // Same-mode re-request fully covered by held locks -> granted.
        if mode == Mode::Shared
            && c.txn(txn)?
                .held
                .iter()
                .any(|h| h.mode == Mode::Shared && h.interval.covers(&interval))
        {
            return Ok(LockOutcome::Granted);
        }
        if c.txn(txn)?.held.contains(&req) {
            return Ok(LockOutcome::Granted);
        }

        if c.can_grant(&req) {
            c.grant(req);
            return Ok(LockOutcome::Granted);
        }

        // Enqueue and check for a deadlock involving this request.
        c.queue.push(req);
        c.log(format!(
            "T{} waits for {:?} [{},{})",
            txn, mode, interval.start, interval.end
        ));
        if let Some(cycle) = c.find_cycle_from(txn) {
            // Deterministic victim: numerically largest txn id in the cycle.
            let victim = *cycle.iter().max().unwrap();
            c.log(format!(
                "deadlock detected: cycle {:?}, victim T{}",
                cycle, victim
            ));
            c.abort_inner(victim, "deadlock victim");
            // After the victim released everything, our request may now be
            // grantable (process_queue already ran inside abort_inner).
            return Ok(LockOutcome::Deadlock { victim, cycle });
        }
        let waiting_for: Vec<u64> = c
            .blockers(&req, c.queue.len())
            .into_iter()
            .collect();
        Ok(LockOutcome::Waiting { waiting_for })
    }

    /// Blocking lock request: waits until the lock is granted, the transaction
    /// is aborted (e.g. chosen as a deadlock victim), or `timeout` elapses.
    pub async fn lock_blocking(
        &self,
        txn: u64,
        mode: Mode,
        interval: Interval,
        timeout: Duration,
    ) -> Result<LockOutcome, LockError> {
        // Fast path under the lock.
        let notify = {
            let mut c = self.guard();
            c.txn_active(txn)?;
            match self.lock_inner(&mut c, txn, mode, interval)? {
                LockOutcome::Waiting { .. } => c.txn(txn)?.notify.clone(),
                LockOutcome::Deadlock { .. } => {
                    // A deadlock was resolved. If this transaction was the
                    // victim it is aborted; otherwise its request either was
                    // granted in the cascade or is still queued (keep waiting).
                    match c.txn(txn)?.state {
                        TxnState::Aborted => return Err(LockError::Aborted),
                        TxnState::Committed => {
                            return Err(LockError::TxnNotActive {
                                state: TxnState::Committed,
                            })
                        }
                        TxnState::Active => {}
                    }
                    if c.waiting_request(txn).is_some() {
                        c.txn(txn)?.notify.clone()
                    } else {
                        return Ok(LockOutcome::Granted);
                    }
                }
                other => return Ok(other),
            }
        };
        // Slow path: wait to be woken, then re-check. Registration with the
        // Notify happens *while holding the mutex* (`enable()`), so a wakeup
        // can never be lost between the state check and going to sleep.
        let wait = async {
            loop {
                let notified = notify.notified();
                tokio::pin!(notified);
                {
                    let c = self.guard();
                    let t = c.txn(txn)?;
                    match t.state {
                        TxnState::Aborted => return Err(LockError::Aborted),
                        TxnState::Committed => {
                            return Err(LockError::TxnNotActive {
                                state: TxnState::Committed,
                            })
                        }
                        TxnState::Active => {}
                    }
                    if c.waiting_request(txn).is_none() {
                        // No longer queued: the request must have been granted.
                        return Ok(LockOutcome::Granted);
                    }
                    notified.as_mut().enable();
                }
                notified.await;
            }
        };
        match tokio::time::timeout(timeout, wait).await {
            Ok(r) => r,
            Err(_) => {
                // Timeout cancels the pending request: leave no orphan waiter.
                let mut c = self.guard();
                if c.waiting_request(txn).is_some() {
                    c.remove_from_queue(txn);
                    c.log(format!("T{} lock request timed out, dequeued", txn));
                    c.process_queue();
                }
                Err(LockError::Timeout)
            }
        }
    }

    /// Release one exactly-matching held lock. Re-processes the queue.
    pub fn unlock(&self, txn: u64, mode: Mode, interval: Interval) -> Result<(), LockError> {
        let mut c = self.guard();
        c.txn_active(txn)?;
        let req = LockRequest { txn, mode, interval };
        let t = c.txns.get_mut(&txn).unwrap();
        if let Some(pos) = t.held.iter().position(|h| *h == req) {
            t.held.remove(pos);
        } else {
            return Err(LockError::LockNotHeld);
        }
        c.log(format!(
            "T{} released {:?} [{},{})",
            txn, mode, interval.start, interval.end
        ));
        c.process_queue();
        Ok(())
    }

    /// Snapshot of the whole manager state.
    pub fn state(&self) -> StateView {
        let c = self.guard();
        let edges = c.wait_for_edges();
        let txns = c
            .txns
            .values()
            .map(|t| {
                let waiting = c.waiting_request(t.id).copied();
                let waiting_for = waiting
                    .map(|req| {
                        let pos = c.queue.iter().position(|q| *q == req).unwrap_or(c.queue.len());
                        c.blockers(&req, pos).into_iter().collect()
                    })
                    .unwrap_or_default();
                TxnView {
                    id: t.id,
                    state: t.state,
                    held: t.held.clone(),
                    waiting,
                    waiting_for,
                }
            })
            .collect();
        StateView {
            txns,
            queue: c.queue.clone(),
            wait_for: edges,
            log: c.log.clone(),
        }
    }

    /// Test helper: wipe everything (used by integration tests / demos).
    pub fn reset(&self) {
        let mut c = self.guard();
        c.txns.clear();
        c.queue.clear();
        c.next_txn = 1;
        c.log.clear();
    }
}
