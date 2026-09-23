//! Single-machine MVCC engine on top of [`crate::store::Store`].
//!
//! Semantics:
//! - *Snapshot reads*: [`ReadTxn`] is pinned to one committed version and sees a
//!   consistent keyspace no matter what later transactions commit.
//! - *First-committer-wins*: two [`WriteTxn`]s started from the same snapshot
//!   conflict if both touch the same key and the first one commits; the second
//!   commit is rejected with [`CommitError::Conflict`] before touching disk.
//! - *Monotonic versions*: every successful commit is published under
//!   `latest + 1`; publication is one fsynced CMMT frame (see [`crate::store`]).
//! - *Reclamation*: garbage collection may remove only records that no active
//!   snapshot can still read. For each key it keeps the newest record below the
//!   watermark (the oldest active snapshot's version) and every record at or
//!   above it.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::store::{GcReport, Mutation, ParsedFile, Record, Store};

/// A scheduled interception point used by deterministic tests: invoked after
/// conflict checking, immediately before a commit becomes durable. Set from
/// tests (never from the HTTP server) to force a particular interleaving.
pub type CommitGate = Arc<dyn Fn(u64) + Send + Sync>;

#[derive(Debug)]
pub enum CommitError {
    /// Snapshot isolation violation; the transaction must be retried.
    Conflict { keys: Vec<Vec<u8>> },
    Io(io::Error),
}

impl std::fmt::Display for CommitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CommitError::Conflict { keys } => write!(
                f,
                "write-write conflict on {} key(s)",
                keys.len()
            ),
            CommitError::Io(e) => write!(f, "I/O error: {e}"),
        }
    }
}
impl std::error::Error for CommitError {}

impl From<io::Error> for CommitError {
    fn from(e: io::Error) -> Self {
        CommitError::Io(e)
    }
}

/// Newest record per key in the committed history.
#[derive(Clone)]
struct KeyEntry {
    version: u64,
    value: Option<Vec<u8>>,
}

#[derive(Debug, Clone, Copy)]
pub struct SnapshotInfo {
    pub id: u64,
    pub version: u64,
}

#[derive(Debug, Clone)]
pub struct Stats {
    pub latest_version: u64,
    pub committed_versions: usize,
    pub live_keys: usize,
    pub snapshots: Vec<SnapshotInfo>,
    pub watermark: u64,
    pub file_bytes: u64,
    /// Bytes a GC pass at the current watermark would retain.
    pub retained_bytes: u64,
    /// Bytes a GC pass at the current watermark would reclaim.
    pub reclaimable_bytes: u64,
}

struct Inner {
    store: Store,
    /// Newest committed record per key (mutated on commit and GC).
    current: BTreeMap<Vec<u8>, KeyEntry>,
    /// Full committed history per key (newest last), used for snapshot reads
    /// and GC planning.
    history: HashMap<Vec<u8>, Vec<KeyEntry>>,
    latest: u64,
    snapshots: BTreeMap<u64, u64>, // snapshot id -> pinned version
    next_snapshot_id: u64,
    gate: Option<CommitGate>,
}

impl Inner {
    fn watermark(&self) -> u64 {
        // No readers: even the newest committed version's record can be
        // replaced by the baseline copy, so the floor is latest + 1.
        self.snapshots
            .values()
            .copied()
            .min()
            .unwrap_or(self.latest + 1)
    }

    fn read_at(&self, key: &[u8], version: u64) -> Option<&KeyEntry> {
        let hist = self.history.get(key)?;
        hist.iter()
            .rev()
            .find(|e| e.version <= version)
    }
}

#[derive(Clone)]
pub struct Engine {
    inner: Arc<Mutex<Inner>>,
    dir: PathBuf,
}

pub struct ReadTxn {
    engine: Engine,
    id: u64,
    version: u64,
    released: bool,
}

pub struct WriteTxn {
    engine: Engine,
    snapshot: u64,
    writes: BTreeMap<Vec<u8>, Option<Vec<u8>>>,
    touched: BTreeSet<Vec<u8>>,
}

impl Engine {
    pub fn open(dir: &Path) -> io::Result<Engine> {
        Engine::open_with_io(Arc::new(crate::io::StdIo), dir)
    }

    pub fn open_with_io(io: Arc<dyn crate::io::Io>, dir: &Path) -> io::Result<Engine> {
        Engine::open_with_io_and_gate(io, dir, None)
    }

    pub fn open_with_io_and_gate(
        io: Arc<dyn crate::io::Io>,
        dir: &Path,
        gate: Option<CommitGate>,
    ) -> io::Result<Engine> {
        let (store, parsed) = Store::open(io, dir)?;
        let dir = dir.to_path_buf();
        let mut inner = Inner {
            store,
            current: BTreeMap::new(),
            history: HashMap::new(),
            latest: 0,
            snapshots: BTreeMap::new(),
            next_snapshot_id: 1,
            gate,
        };
        rebuild_index(&mut inner, &parsed);
        Ok(Engine {
            inner: Arc::new(Mutex::new(inner)),
            dir,
        })
    }

    pub fn dir(&self) -> &Path {
        &self.dir
    }

    pub fn latest_version(&self) -> u64 {
        self.inner.lock().unwrap().latest
    }

    /// Begin a read transaction pinned at the newest committed version.
    pub fn begin_read(&self) -> ReadTxn {
        let mut g = self.inner.lock().unwrap();
        let id = g.next_snapshot_id;
        g.next_snapshot_id += 1;
        let version = g.latest;
        g.snapshots.insert(id, version);
        ReadTxn {
            engine: self.clone(),
            id,
            version,
            released: false,
        }
    }

    /// Begin a write transaction whose snapshot is the newest committed
    /// version.
    pub fn begin_write(&self) -> WriteTxn {
        let snapshot = self.inner.lock().unwrap().latest;
        WriteTxn {
            engine: self.clone(),
            snapshot,
            writes: BTreeMap::new(),
            touched: BTreeSet::new(),
        }
    }

    /// Single-shot consistent read at the newest committed version (no
    /// long-lived snapshot; useful for simple gets).
    pub fn get_latest(&self, key: &[u8]) -> Option<Vec<u8>> {
        let g = self.inner.lock().unwrap();
        g.read_at(key, g.latest).and_then(|e| e.value.clone())
    }

    /// Current garbage-collection watermark (oldest pinned snapshot version).
    pub fn watermark(&self) -> u64 {
        self.inner.lock().unwrap().watermark()
    }

    /// Versions recorded as committed in the on-disk log right now. Exposed for
    /// tests and operational inspection; reflects the current file, so it
    /// shrinks after GC.
    pub fn committed_versions_on_disk(&self) -> io::Result<Vec<u64>> {
        let g = self.inner.lock().unwrap();
        Ok(g.store.parse_current()?.versions)
    }

    pub fn stats(&self) -> Stats {
        let g = self.inner.lock().unwrap();
        let parsed = g.store.parse_current().unwrap_or_default();
        let wm = g.watermark();

        let mut keep: BTreeSet<u64> = BTreeSet::new();
        plan_keep(&parsed, wm, &mut keep);

        let all_offsets: BTreeSet<u64> = parsed.records.iter().map(|r| r.offset).collect();
        let retained_bytes = if keep == all_offsets {
            // Nothing would be rewritten: the file is already minimal.
            parsed.size
        } else {
            let kept_records: Vec<&Record> =
                parsed.records.iter().filter(|r| keep.contains(&r.offset)).collect();
            let frame_bytes: u64 = kept_records.iter().map(|r| r.frame_len).sum();
            let version_count =
                kept_records.iter().map(|r| r.version).collect::<BTreeSet<_>>().len();
            crate::store::HEADER_LEN
                + frame_bytes
                + crate::store::vset_manifest_len(version_count)
        };

        let mut snapshots: Vec<SnapshotInfo> = g
            .snapshots
            .iter()
            .map(|(id, v)| SnapshotInfo { id: *id, version: *v })
            .collect();
        snapshots.sort_by_key(|s| s.id);

        Stats {
            latest_version: g.latest,
            committed_versions: parsed.versions.len(),
            live_keys: g.current.values().filter(|e| e.value.is_some()).count(),
            snapshots,
            watermark: wm,
            file_bytes: parsed.size,
            retained_bytes,
            reclaimable_bytes: parsed.size.saturating_sub(retained_bytes),
        }
    }

    /// Run garbage collection at the current watermark. Returns `None` when
    /// there is nothing to reclaim.
    pub fn gc(&self) -> Result<Option<GcReport>, CommitError> {
        let mut g = self.inner.lock().unwrap();
        let parsed = g.store.parse_current()?;
        let wm = g.watermark();

        let mut keep = BTreeSet::new();
        plan_keep(&parsed, wm, &mut keep);

        let all_records: BTreeSet<u64> = parsed.records.iter().map(|r| r.offset).collect();
        if keep == all_records {
            return Ok(None);
        }

        let (report, new_parsed) = g.store.compact(&keep, wm)?;
        rebuild_index(&mut g, &new_parsed);
        Ok(Some(report))
    }

    fn release_snapshot(&self, id: u64) {
        self.inner.lock().unwrap().snapshots.remove(&id);
    }
}

/// Choose which data records must survive compaction at `watermark`.
///
/// - every committed record with `version >= watermark`;
/// - for each key, the newest committed record with `version < watermark`.
fn plan_keep(parsed: &ParsedFile, watermark: u64, keep: &mut BTreeSet<u64>) {
    let committed: BTreeSet<u64> = parsed.versions.iter().copied().collect();
    // Newest (highest (version, offset)) record strictly below the watermark.
    let mut floor: HashMap<Vec<u8>, (u64, u64)> = HashMap::new();

    for r in &parsed.records {
        if !committed.contains(&r.version) {
            continue; // uncommitted tail: always discarded
        }
        if r.version >= watermark {
            keep.insert(r.offset);
        } else {
            match floor.get(&r.key) {
                Some((v, o)) if (r.version, r.offset) <= (*v, *o) => {}
                _ => {
                    floor.insert(r.key.clone(), (r.version, r.offset));
                }
            }
        }
    }
    for (_, (_, offset)) in floor {
        keep.insert(offset);
    }
}

fn rebuild_index(g: &mut Inner, parsed: &ParsedFile) {
    g.current.clear();
    g.history.clear();

    // Records appear in commit order; insert committed ones into per-key
    // history (newest last).
    for r in &parsed.records {
        if !parsed.versions.contains(&r.version) {
            continue;
        }
        let entry = KeyEntry {
            version: r.version,
            value: r.value.clone(),
        };
        let hist = g.history.entry(r.key.clone()).or_default();
        // Defensive: after compaction records are ordered by (version,
        // offset); in the raw log a version's records are one contiguous
        // block, so strictly greater versions simply append.
        match hist.binary_search_by(|e: &KeyEntry| e.version.cmp(&r.version)) {
            Ok(_) => {
                // Same version + key appears at most once; ignore duplicates.
            }
            Err(idx) => hist.insert(idx, entry.clone()),
        }
        g.current.insert(r.key.clone(), entry);
    }

    g.latest = parsed.versions.last().copied().unwrap_or(0);
}

// ---------------------------------------------------------------------------
// Transactions
// ---------------------------------------------------------------------------

impl ReadTxn {
    pub fn version(&self) -> u64 {
        self.version
    }

    pub fn id(&self) -> u64 {
        self.id
    }

    pub fn get(&self, key: &[u8]) -> Option<Vec<u8>> {
        let g = self.engine.inner.lock().unwrap();
        g.read_at(key, self.version).and_then(|e| e.value.clone())
    }

    pub fn exists(&self, key: &[u8]) -> bool {
        let g = self.engine.inner.lock().unwrap();
        matches!(g.read_at(key, self.version), Some(e) if e.value.is_some())
    }

    /// Release the snapshot early so GC can reclaim older versions. Idempotent;
    /// also called by [`Drop`].
    pub fn release(mut self) {
        self.do_release();
    }

    fn do_release(&mut self) {
        if !self.released {
            self.engine.release_snapshot(self.id);
            self.released = true;
        }
    }
}

impl Drop for ReadTxn {
    fn drop(&mut self) {
        self.do_release();
    }
}

impl WriteTxn {
    pub fn snapshot_version(&self) -> u64 {
        self.snapshot
    }

    pub fn put(&mut self, key: impl Into<Vec<u8>>, value: impl Into<Vec<u8>>) {
        let key = key.into();
        self.touched.insert(key.clone());
        self.writes.insert(key, Some(value.into()));
    }

    pub fn del(&mut self, key: impl Into<Vec<u8>>) {
        let key = key.into();
        self.touched.insert(key.clone());
        self.writes.insert(key, None);
    }

    /// Read-your-writes: local buffer first, otherwise the pinned snapshot.
    pub fn get(&self, key: &[u8]) -> Option<Vec<u8>> {
        if let Some(v) = self.writes.get(key) {
            return v.clone();
        }
        let g = self.engine.inner.lock().unwrap();
        g.read_at(key, self.snapshot).and_then(|e| e.value.clone())
    }

    pub fn commit(self) -> Result<u64, CommitError> {
        self.try_commit()
    }

    fn try_commit(&self) -> Result<u64, CommitError> {
        let mut g = self.engine.inner.lock().unwrap();

        // 1. First-committer-wins conflict check against everything committed
        //    after this transaction's snapshot.
        let mut conflicts = Vec::new();
        for key in &self.touched {
            if let Some(head) = g.current.get(key) {
                if head.version > self.snapshot {
                    conflicts.push(key.clone());
                }
            }
        }
        if !conflicts.is_empty() {
            conflicts.sort();
            return Err(CommitError::Conflict { keys: conflicts });
        }

        let new_version = g.latest + 1;

        // 2. Deterministic scheduling hook (tests only): still under the
        //    serialization lock, so another committer cannot sneak past.
        if let Some(gate) = &g.gate {
            gate(new_version);
        }

        // 3. Materialize: data frames (unsynced) then one fsynced CMMT.
        let mut mutations: Vec<Mutation> = Vec::with_capacity(self.writes.len());
        for (k, v) in &self.writes {
            mutations.push(Mutation {
                key: k.clone(),
                value: v.clone(),
            });
        }
        mutations.sort_by(|a, b| a.key.cmp(&b.key));

        let first = g.store.append_mutations(new_version, &mutations)?;
        g.store.publish(new_version, first)?;

        // 4. Publish in memory only after durability.
        for m in &mutations {
            let entry = KeyEntry {
                version: new_version,
                value: m.value.clone(),
            };
            g.history
                .entry(m.key.clone())
                .or_default()
                .push(entry.clone());
            g.current.insert(m.key.clone(), entry);
        }
        g.latest = new_version;
        Ok(new_version)
    }
}
