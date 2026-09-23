//! In-memory MVCC storage engine.
//!
//! Timestamp model:
//! - `commit_ts` values are dense integers 1, 2, 3, ... assigned at commit time.
//! - A transaction started at `start_ts = last_commit_ts` reads the committed
//!   state as of that timestamp: a version with `commit_ts <= start_ts` is
//!   visible (the newest such version per key wins).
//! - Deletes are recorded as tombstone versions, never by mutating history, so
//!   older readers keep seeing the deleted value.
//!
//! Garbage collection:
//! - The watermark is the minimum `start_ts` over all live transactions and
//!   open snapshots (no live readers => watermark = current last commit ts).
//! - For each key, GC keeps every version with `commit_ts > watermark`, plus
//!   the newest version with `commit_ts <= watermark` (that single version is
//!   enough to answer every reader at or below the watermark).
//! - Therefore a long-running snapshot pins every version it depends on until
//!   it is closed; the shared version chains are never copied.

use std::collections::{BTreeMap, HashMap};
use std::fmt;
use std::sync::{Mutex, MutexGuard};

use serde::Serialize;

/// Monotonic commit timestamp; 0 means "nothing committed yet".
pub type Ts = u64;
pub type TxnId = u64;
pub type SnapshotId = u64;

/// One committed version of a key.
#[derive(Debug, Clone)]
pub struct Version {
    pub ts: Ts,
    /// `None` means a tombstone (the key was deleted at this timestamp).
    pub value: Option<serde_json::Value>,
}

#[derive(Debug, Clone)]
struct TxnState {
    start_ts: Ts,
    /// Keys written by this transaction, each mapping to a value or a tombstone.
    writes: HashMap<String, Option<serde_json::Value>>,
}

#[derive(Debug, Clone, Copy)]
struct SnapshotState {
    ts: Ts,
}

#[derive(Debug, Default, Clone, Serialize)]
pub struct GcReport {
    /// Versions that were dropped.
    pub versions_removed: usize,
    /// Tombstone versions that were dropped (subset of `versions_removed`).
    pub tombstones_removed: usize,
    /// Keys whose whole (tombstone-only) chain was dropped.
    pub keys_removed: usize,
    /// Watermark used for this GC pass.
    pub watermark: Ts,
    /// Number of live transactions pinning the watermark.
    pub active_txns: usize,
    /// Number of open snapshots pinning the watermark.
    pub active_snapshots: usize,
}

pub struct Db {
    inner: Mutex<Inner>,
}

struct Inner {
    /// Per-key version chains, sorted ascending by commit timestamp.
    versions: BTreeMap<String, Vec<Version>>,
    txns: HashMap<TxnId, TxnState>,
    snapshots: HashMap<SnapshotId, SnapshotState>,
    /// Highest assigned commit timestamp.
    last_ts: Ts,
    next_txn_id: TxnId,
    next_snapshot_id: SnapshotId,
}

impl Default for Db {
    fn default() -> Self {
        Self::new()
    }
}

impl Db {
    pub fn new() -> Self {
        Db {
            inner: Mutex::new(Inner {
                versions: BTreeMap::new(),
                txns: HashMap::new(),
                snapshots: HashMap::new(),
                last_ts: 0,
                next_txn_id: 1,
                next_snapshot_id: 1,
            }),
        }
    }

    fn lock(&self) -> MutexGuard<'_, Inner> {
        self.inner.lock().unwrap()
    }

    /// Begin a transaction reading at the current last commit timestamp.
    pub fn begin(&self) -> (TxnId, Ts) {
        let mut g = self.lock();
        let id = g.next_txn_id;
        g.next_txn_id += 1;
        let start_ts = g.last_ts;
        g.txns.insert(
            id,
            TxnState {
                start_ts,
                writes: HashMap::new(),
            },
        );
        (id, start_ts)
    }

    /// Open a read-only snapshot. With `ts == None` it reads the latest
    /// committed state; an explicit timestamp must not be in the future.
    pub fn snapshot_open(&self, ts: Option<Ts>) -> Result<(SnapshotId, Ts), MvccError> {
        let mut g = self.lock();
        if let Some(ts) = ts {
            if ts > g.last_ts {
                return Err(MvccError::InvalidSnapshotTs {
                    requested: ts,
                    max: g.last_ts,
                });
            }
        }
        let id = g.next_snapshot_id;
        g.next_snapshot_id += 1;
        let ts = ts.unwrap_or(g.last_ts);
        g.snapshots.insert(id, SnapshotState { ts });
        Ok((id, ts))
    }

    pub fn snapshot_close(&self, id: SnapshotId) -> Result<(), MvccError> {
        let mut g = self.lock();
        g.snapshots
            .remove(&id)
            .ok_or(MvccError::SnapshotNotFound(id))
            .map(|_| ())
    }

    /// Read a key inside a transaction: snapshot read plus read-your-writes.
    pub fn get(&self, txn_id: TxnId, key: &str) -> Result<Option<serde_json::Value>, MvccError> {
        let g = self.lock();
        let txn = g.txns.get(&txn_id).ok_or(MvccError::TxnNotFound(txn_id))?;
        if let Some(v) = txn.writes.get(key) {
            return Ok(v.clone());
        }
        Ok(read_version(&g.versions, key, txn.start_ts))
    }

    pub fn put(
        &self,
        txn_id: TxnId,
        key: String,
        value: serde_json::Value,
    ) -> Result<(), MvccError> {
        let mut g = self.lock();
        g.txns
            .get_mut(&txn_id)
            .ok_or(MvccError::TxnNotFound(txn_id))?
            .writes
            .insert(key, Some(value));
        Ok(())
    }

    pub fn delete(&self, txn_id: TxnId, key: String) -> Result<(), MvccError> {
        let mut g = self.lock();
        g.txns
            .get_mut(&txn_id)
            .ok_or(MvccError::TxnNotFound(txn_id))?
            .writes
            .insert(key, None);
        Ok(())
    }

    /// Commit with first-committer-wins write-write conflict detection.
    /// Returns the assigned commit timestamp (the start timestamp is returned
    /// unchanged for read-only transactions, which take no new timestamp).
    pub fn commit(&self, txn_id: TxnId) -> Result<Ts, MvccError> {
        let mut g = self.lock();
        let txn = g
            .txns
            .remove(&txn_id)
            .ok_or(MvccError::TxnNotFound(txn_id))?;

        if txn.writes.is_empty() {
            return Ok(txn.start_ts);
        }

        // Conflict: any key in the write set gained a version after this
        // transaction started (committed by a concurrent transaction).
        for key in txn.writes.keys() {
            if let Some(chain) = g.versions.get(key) {
                if chain.last().is_some_and(|v| v.ts > txn.start_ts) {
                    return Err(MvccError::WriteConflict { key: key.clone() });
                }
            }
        }

        g.last_ts += 1;
        let commit_ts = g.last_ts;
        for (key, value) in txn.writes {
            g.versions.entry(key).or_default().push(Version {
                ts: commit_ts,
                value,
            });
        }
        Ok(commit_ts)
    }

    pub fn abort(&self, txn_id: TxnId) -> Result<(), MvccError> {
        let mut g = self.lock();
        g.txns
            .remove(&txn_id)
            .ok_or(MvccError::TxnNotFound(txn_id))
            .map(|_| ())
    }

    pub fn snapshot_read(
        &self,
        snapshot_id: SnapshotId,
        key: &str,
    ) -> Result<Option<serde_json::Value>, MvccError> {
        let g = self.lock();
        let snap = g
            .snapshots
            .get(&snapshot_id)
            .ok_or(MvccError::SnapshotNotFound(snapshot_id))?;
        Ok(read_version(&g.versions, key, snap.ts))
    }

    pub fn txn_status(&self, txn_id: TxnId) -> Result<TxnStatus, MvccError> {
        let g = self.lock();
        let txn = g.txns.get(&txn_id).ok_or(MvccError::TxnNotFound(txn_id))?;
        Ok(TxnStatus {
            id: txn_id,
            start_ts: txn.start_ts,
            writes: txn.writes.keys().cloned().collect(),
        })
    }

    pub fn snapshot_status(&self, snapshot_id: SnapshotId) -> Result<SnapshotStatus, MvccError> {
        let g = self.lock();
        let snap = g
            .snapshots
            .get(&snapshot_id)
            .ok_or(MvccError::SnapshotNotFound(snapshot_id))?;
        Ok(SnapshotStatus {
            id: snapshot_id,
            ts: snap.ts,
        })
    }

    /// Reclaim versions no live reader can reach.
    pub fn gc(&self) -> GcReport {
        let mut g = self.lock();
        let mut report = GcReport::default();
        report.active_txns = g.txns.len();
        report.active_snapshots = g.snapshots.len();

        let watermark = watermark_of(&g);
        report.watermark = watermark;

        let mut empty_keys = Vec::new();
        let no_live_readers = report.active_txns == 0 && report.active_snapshots == 0;
        for (key, chain) in g.versions.iter_mut() {
            // Keep all versions above the watermark; among versions at or
            // below it keep only the newest.
            let cutoff = chain.partition_point(|v| v.ts <= watermark);
            let keep_from = cutoff.saturating_sub(1);
            if keep_from > 0 {
                for dropped in &chain[..keep_from] {
                    report.versions_removed += 1;
                    if dropped.value.is_none() {
                        report.tombstones_removed += 1;
                    }
                }
                chain.drain(..keep_from);
            }
            // A chain whose sole remaining entry is a tombstone is invisible
            // to every reader at/below the watermark; dropping it changes no
            // read result (an absent key reads as deleted). We only do this
            // when no live reader exists at all, so that an open snapshot
            // keeps every version its reads depend on until it is closed.
            if no_live_readers && chain.len() == 1 && chain[0].value.is_none() {
                report.versions_removed += 1;
                report.tombstones_removed += 1;
                empty_keys.push(key.clone());
                chain.clear();
            }
        }
        for key in empty_keys {
            g.versions.remove(&key);
            report.keys_removed += 1;
        }
        report
    }

    pub fn stats(&self) -> Stats {
        let g = self.lock();
        let total_versions: usize = g.versions.values().map(|c| c.len()).sum();
        let total_tombstones: usize = g
            .versions
            .values()
            .flat_map(|c| c.iter())
            .filter(|v| v.value.is_none())
            .count();
        Stats {
            last_commit_ts: g.last_ts,
            active_txns: g.txns.len(),
            active_snapshots: g.snapshots.len(),
            keys: g.versions.len(),
            total_versions,
            total_tombstones,
            watermark: watermark_of(&g),
        }
    }

    pub fn key_versions(&self, key: &str) -> Vec<VersionJson> {
        let g = self.lock();
        match g.versions.get(key) {
            Some(chain) => chain
                .iter()
                .map(|v| VersionJson {
                    ts: v.ts,
                    deleted: v.value.is_none(),
                    value: v.value.clone(),
                })
                .collect(),
            None => Vec::new(),
        }
    }
}

/// Visible value of `key` as of `as_of`: newest version with `ts <= as_of`.
fn read_version(
    versions: &BTreeMap<String, Vec<Version>>,
    key: &str,
    as_of: Ts,
) -> Option<serde_json::Value> {
    let chain = versions.get(key)?;
    let idx = chain.partition_point(|v| v.ts <= as_of);
    // No version at or below the reader's timestamp (e.g. a reader that
    // started at ts=0 while the first version committed at ts=1).
    if idx == 0 {
        return None;
    }
    // idx is the first entry with ts > as_of; the entry before it wins.
    chain[idx - 1].value.clone()
}

/// Minimum start timestamp over all live readers; with no readers, the
/// current last commit timestamp (every committed version is "old").
fn watermark_of(g: &Inner) -> Ts {
    let mut w = g.last_ts;
    for txn in g.txns.values() {
        w = w.min(txn.start_ts);
    }
    for snap in g.snapshots.values() {
        w = w.min(snap.ts);
    }
    w
}

#[derive(Debug, Clone, Serialize)]
pub struct TxnStatus {
    pub id: TxnId,
    pub start_ts: Ts,
    pub writes: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct SnapshotStatus {
    pub id: SnapshotId,
    pub ts: Ts,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub last_commit_ts: Ts,
    pub active_txns: usize,
    pub active_snapshots: usize,
    pub keys: usize,
    pub total_versions: usize,
    pub total_tombstones: usize,
    pub watermark: Ts,
}

#[derive(Debug, Clone, Serialize)]
pub struct VersionJson {
    pub ts: Ts,
    pub deleted: bool,
    pub value: Option<serde_json::Value>,
}

#[derive(Debug)]
pub enum MvccError {
    TxnNotFound(TxnId),
    SnapshotNotFound(SnapshotId),
    WriteConflict { key: String },
    InvalidSnapshotTs { requested: Ts, max: Ts },
}

impl fmt::Display for MvccError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            MvccError::TxnNotFound(id) => write!(f, "transaction {id} not found"),
            MvccError::SnapshotNotFound(id) => write!(f, "snapshot {id} not found"),
            MvccError::WriteConflict { key } => write!(
                f,
                "write-write conflict on key {key:?}: a concurrent transaction committed a newer version"
            ),
            MvccError::InvalidSnapshotTs { requested, max } => write!(
                f,
                "requested snapshot timestamp {requested} is beyond the latest commit timestamp {max}"
            ),
        }
    }
}

impl std::error::Error for MvccError {}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn engine_snapshot_pins_gc_watermark_directly() {
        let db = Db::new();
        let (w, wts) = db.begin();
        assert_eq!(wts, 0);
        // empty read through the txn, then commit history from other txns
        assert_eq!(db.get(w, "k").unwrap(), None);

        let (t, _) = db.begin();
        db.put(t, "k".into(), json!(1)).unwrap();
        assert_eq!(db.commit(t).unwrap(), 1);
        let (t, _) = db.begin();
        db.put(t, "k".into(), json!(2)).unwrap();
        assert_eq!(db.commit(t).unwrap(), 2);
        let (t, _) = db.begin();
        db.delete(t, "k".into()).unwrap();
        assert_eq!(db.commit(t).unwrap(), 3);

        // the writer at start_ts=0 still reads nothing: it began before any
        // commit, so even the first version is invisible to it
        assert_eq!(db.get(w, "k").unwrap(), None);

        let (s, sts) = db.snapshot_open(Some(1)).unwrap();
        assert_eq!(sts, 1);
        assert_eq!(db.snapshot_read(s, "k").unwrap(), Some(json!(1)));

        // both readers pin the watermark (min(0, 1) = 0); nothing below
        // the watermark exists, so nothing is removed
        let r = db.gc();
        assert_eq!(r.watermark, 0);
        assert_eq!(r.versions_removed, 0);
        assert_eq!(db.key_versions("k").len(), 3);

        // abort the ts0 txn -> watermark rises to 1; the newest version at
        // or below 1 is v1@1, and every newer version (v2@2, tombstone@3)
        // is above the watermark, so the whole 3-entry chain is still kept
        db.abort(w).unwrap();
        let r = db.gc();
        assert_eq!(r.watermark, 1);
        assert_eq!(r.versions_removed, 0);
        assert_eq!(db.snapshot_read(s, "k").unwrap(), Some(json!(1)));

        // close the last reader -> no live readers, watermark 3: v1@1 and
        // v2@2 are reclaimed; the lone surviving entry is the tombstone@3,
        // which is removed together with the empty key in the same pass
        db.snapshot_close(s).unwrap();
        let r = db.gc();
        assert_eq!(r.watermark, 3);
        assert_eq!(r.versions_removed, 3);
        assert_eq!(r.tombstones_removed, 1);
        assert_eq!(r.keys_removed, 1);
        assert_eq!(db.key_versions("k").len(), 0);
        // semantics preserved: deleted reads as absent
        let (t, _) = db.begin();
        assert_eq!(db.get(t, "k").unwrap(), None);
    }

    #[test]
    fn engine_conflict_on_overwrite_and_delete_alike() {
        let db = Db::new();
        let (seed, _) = db.begin();
        db.put(seed, "a".into(), json!(1)).unwrap();
        db.commit(seed).unwrap(); // ts1

        let (t1, s1) = db.begin();
        let (t2, s2) = db.begin();
        assert_eq!((s1, s2), (1, 1));

        db.put(t1, "a".into(), json!(2)).unwrap();
        // t2 conflicts whether it overwrites or deletes
        db.delete(t2, "a".into()).unwrap();

        assert_eq!(db.commit(t1).unwrap(), 2);
        match db.commit(t2) {
            Err(MvccError::WriteConflict { key }) => assert_eq!(key, "a"),
            other => panic!("expected WriteConflict, got {other:?}"),
        }
        // loser txn is gone
        assert!(matches!(db.abort(t2), Err(MvccError::TxnNotFound(_))));
    }

    #[test]
    fn engine_disjoint_write_sets_both_commit() {
        let db = Db::new();
        let (t1, _) = db.begin();
        let (t2, _) = db.begin();
        db.put(t1, "x".into(), json!(1)).unwrap();
        db.put(t2, "y".into(), json!(2)).unwrap();
        assert_eq!(db.commit(t1).unwrap(), 1);
        assert_eq!(db.commit(t2).unwrap(), 2); // no conflict: different keys
        let (r, _) = db.begin();
        assert_eq!(db.get(r, "x").unwrap(), Some(json!(1)));
        assert_eq!(db.get(r, "y").unwrap(), Some(json!(2)));
    }

    #[test]
    fn engine_writes_buffered_until_commit_are_invisible_to_others() {
        let db = Db::new();
        let (t1, _) = db.begin();
        let (t2, _) = db.begin();
        db.put(t1, "k".into(), json!("buffered")).unwrap();
        // t2 cannot see t1's uncommitted write
        assert_eq!(db.get(t2, "k").unwrap(), None);
        db.abort(t1).unwrap();
        assert_eq!(db.get(t2, "k").unwrap(), None);
    }
}
