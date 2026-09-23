//! Snapshot-isolation MVCC engine with snapshot-aware version reclamation.
//!
//! ## Concurrency model
//!
//! The store is single-process. A single `Mutex` serializes the *state*
//! transitions; the only slow operation performed while holding it is the
//! final `fsync` of a commit (commit is a critical section by design:
//! versions are allocated in commit order). Reads from long-lived snapshots
//! do not take the lock across I/O — they resolve an in-memory version
//! index, while the bytes for every version they may see are protected from
//! reclamation by the snapshot's pin (see [`Engine::gc`]).
//!
//! ## Versions
//!
//! Every successful commit allocates the next monotonic `u64` version.
//! Transactions and explicit snapshots read at the version that was current
//! when they started.
//!
//! ## Reclamation invariant
//!
//! `gc()` compacts to a *horizon* `H` = the minimum read version of every
//! active transaction and explicit snapshot (or the latest committed
//! version when nothing is active). It writes one base file containing the
//! visible value of every key at `H`, and only then deletes files whose
//! version is `<= H`. Because no active reader reads at a version below
//! `H`, no version still needed is ever deleted; conversely, when a long
//! reader pins an old version, old segments are retained until the pin is
//! released.

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use crate::error::{Error, Result};
use crate::format::{self, Cell, Segment};
use crate::vfs::{FaultVfs, SharedVfs};

/// One committed version of one key (value `None` = delete tombstone).
#[derive(Debug, Clone, PartialEq, Eq)]
struct Entry {
    version: u64,
    val: Option<Vec<u8>>,
}

#[derive(Debug, Clone)]
struct FileMeta {
    is_base: bool,
    size: u64,
    /// True once GC has logically replaced this file but the unlink failed.
    stale: bool,
}

/// An open read-write transaction.
struct TxState {
    read_version: u64,
    writes: BTreeMap<Vec<u8>, Option<Vec<u8>>>,
}

/// An explicit long-lived read snapshot pin.
#[derive(Debug, Clone)]
pub struct SnapshotInfo {
    pub id: u64,
    pub version: u64,
}

/// Result of a compaction pass.
#[derive(Debug, Clone)]
pub struct GcReport {
    pub horizon: u64,
    pub base_version_before: u64,
    pub base_written: bool,
    pub base_cells: usize,
    pub files_removed: usize,
    pub bytes_reclaimed: u64,
    /// Obsolete files whose unlink failed; they will be retried on the next
    /// pass or at reopen.
    pub stale_left: usize,
    pub noop_reason: Option<&'static str>,
}

/// Engine statistics, also serialized by the HTTP layer.
#[derive(Debug, Clone)]
pub struct Stats {
    pub current_version: u64,
    pub base_version: u64,
    pub keys: usize,
    pub live_cells: usize,
    pub open_transactions: Vec<(u64, u64)>,
    pub snapshots: Vec<SnapshotInfo>,
    pub segment_files: usize,
    pub base_files: usize,
    pub stale_files: usize,
    pub live_bytes: u64,
    pub stale_bytes: u64,
}

struct Inner {
    root: PathBuf,
    vfs: SharedVfs,
    /// Next version to allocate (current committed version = `next_version - 1`).
    next_version: u64,
    /// Horizon of the newest compacted base (0 = no base yet).
    base_version: u64,
    index: BTreeMap<Vec<u8>, Vec<Entry>>,
    txs: BTreeMap<u64, TxState>,
    snapshots: BTreeMap<u64, SnapshotInfo>,
    /// Every file known on disk, keyed by its version.
    files: BTreeMap<u64, FileMeta>,
    next_tx_id: u64,
    next_snap_id: u64,
}

/// Handle to an open store. Cheap to clone (`Arc` inside).
#[derive(Clone)]
pub struct Engine {
    inner: Arc<Mutex<Inner>>,
    faults: FaultVfs,
}

impl Engine {
    /// Open (or create) a store in `root` on the given file system.
    ///
    /// Single-writer-process contract — see [`Engine::open_faulted`].
    pub fn open(root: impl Into<PathBuf>, vfs: SharedVfs) -> Result<Engine> {
        Self::open_faulted(root, FaultVfs::new(vfs))
    }

    /// Open with an externally constructed [`FaultVfs`], so callers can arm
    /// I/O faults around open/GC as well as commits.
    ///
    /// Single-writer-process contract: opening the same data directory from
    /// two processes at once is unsupported (the store is single-node). The
    /// process-internal mutex makes multiple *threads* safe; nothing here
    /// coordinates separate processes.
    pub fn open_faulted(root: impl Into<PathBuf>, faults: FaultVfs) -> Result<Engine> {
        let root = root.into();
        let vfs: SharedVfs = Arc::new(faults.clone());
        vfs.create_dir_all(&root)?;

        // Clean crash debris from interrupted publications.
        for e in vfs.list_dir(&root)? {
            if format::is_temp_name(&e.name) {
                let _ = vfs.remove_file(&root.join(&e.name));
            }
        }

        // Discover data files.
        let mut on_disk: BTreeMap<u64, FileMeta> = BTreeMap::new();
        let mut newest_base = 0u64;
        for e in vfs.list_dir(&root)? {
            if !e.is_file {
                continue;
            }
            if let Some((version, is_base)) = format::parse_versioned_name(&e.name) {
                let data = format::read_whole(&vfs, &root.join(&e.name))?;
                let _seg: Segment = format::decode_file(&e.name, &data)?;
                if is_base {
                    newest_base = newest_base.max(version);
                }
                on_disk.insert(
                    version,
                    FileMeta {
                        is_base,
                        size: data.len() as u64,
                        stale: false,
                    },
                );
            }
        }

        // Interrupted GC may have left files the new base obsoletes.
        for (&v, meta) in &on_disk {
            if v <= newest_base && !(meta.is_base && v == newest_base) {
                let name = if meta.is_base {
                    format::base_name(v)
                } else {
                    format::segment_name(v)
                };
                let _ = vfs.remove_file(&root.join(name));
            }
        }
        on_disk.retain(|v, m| *v > newest_base || (m.is_base && *v == newest_base));

        // Replay: base first, then surviving segments in version order.
        let mut index: BTreeMap<Vec<u8>, Vec<Entry>> = BTreeMap::new();
        let mut next_version = 1u64;
        for (&v, meta) in &on_disk {
            next_version = next_version.max(v + 1);
            let name = if meta.is_base {
                format::base_name(v)
            } else {
                format::segment_name(v)
            };
            let data = format::read_whole(&vfs, &root.join(&name))?;
            let seg = format::decode_file(&name, &data)?;
            for c in seg.cells {
                apply_cell(
                    &mut index,
                    c.key,
                    Entry {
                        version: c.version,
                        val: c.val,
                    },
                );
            }
        }

        Ok(Engine {
            inner: Arc::new(Mutex::new(Inner {
                root,
                vfs,
                next_version,
                base_version: newest_base,
                index,
                txs: BTreeMap::new(),
                snapshots: BTreeMap::new(),
                files: on_disk,
                next_tx_id: 1,
                next_snap_id: 1,
            })),
            faults,
        })
    }

    /// Fault-injection handle (no-op unless faults are armed).
    pub fn faults(&self) -> &FaultVfs {
        &self.faults
    }

    /// Begin a transaction; returns `(tx_id, read_version)`.
    pub fn begin(&self) -> (u64, u64) {
        let mut g = self.inner.lock().unwrap();
        let id = g.next_tx_id;
        g.next_tx_id += 1;
        let read_version = g.next_version - 1;
        g.txs.insert(
            id,
            TxState {
                read_version,
                writes: BTreeMap::new(),
            },
        );
        (id, read_version)
    }

    /// Pin an explicit long-lived read snapshot at the current version.
    pub fn snapshot(&self) -> SnapshotInfo {
        let mut g = self.inner.lock().unwrap();
        let id = g.next_snap_id;
        g.next_snap_id += 1;
        let info = SnapshotInfo {
            id,
            version: g.next_version - 1,
        };
        g.snapshots.insert(id, info.clone());
        info
    }

    pub fn release_snapshot(&self, id: u64) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        match g.snapshots.remove(&id) {
            Some(_) => Ok(()),
            None => Err(Error::UnknownSnapshot(id)),
        }
    }

    pub fn put(&self, tx: u64, key: Vec<u8>, val: Vec<u8>) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        g.tx_mut(tx)?.writes.insert(key, Some(val));
        Ok(())
    }

    pub fn delete(&self, tx: u64, key: Vec<u8>) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        g.tx_mut(tx)?.writes.insert(key, None);
        Ok(())
    }

    /// Read inside a transaction: own buffered writes win, otherwise the
    /// newest committed version `<=` its read snapshot.
    pub fn get_tx(&self, tx: u64, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let g = self.inner.lock().unwrap();
        let t = g.txs.get(&tx).ok_or(Error::UnknownTx(tx))?;
        if let Some(v) = t.writes.get(key) {
            return Ok(v.clone());
        }
        Ok(read_at(&g.index, key, t.read_version))
    }

    /// Read through an explicit snapshot pin.
    pub fn get_snapshot(&self, snap: u64, key: &[u8]) -> Result<Option<Vec<u8>>> {
        let g = self.inner.lock().unwrap();
        let s = g.snapshots.get(&snap).ok_or(Error::UnknownSnapshot(snap))?;
        Ok(read_at(&g.index, key, s.version))
    }

    /// Abort/discard a transaction.
    pub fn abort(&self, tx: u64) -> Result<()> {
        let mut g = self.inner.lock().unwrap();
        match g.txs.remove(&tx) {
            Some(_) => Ok(()),
            None => Err(Error::UnknownTx(tx)),
        }
    }

    /// Commit: first-writer-wins conflict check, then durable publication
    /// at the next monotonic version.
    ///
    /// On I/O failure nothing is published and the transaction stays open
    /// (the caller may retry the commit or abort).
    pub fn commit(&self, tx: u64) -> Result<u64> {
        // Phase 1: conflict check under the lock, allocate the version and
        // stage the exact bytes to publish.
        let (version, cells, body) = {
            let g = self.inner.lock().unwrap();
            let t = g.txs.get(&tx).ok_or(Error::UnknownTx(tx))?;
            let read_version = t.read_version;
            for key in t.writes.keys() {
                if let Some(entry) = g.index.get(key).and_then(|v| v.last()) {
                    if entry.version > read_version {
                        return Err(Error::Conflict(key.clone()));
                    }
                }
            }
            let version = g.next_version;
            let mut cells: Vec<Cell> = t
                .writes
                .iter()
                .map(|(k, v)| Cell {
                    key: k.clone(),
                    val: v.clone(),
                    version,
                })
                .collect();
            let body = format::encode_segment(version, &mut cells);
            (version, cells, body)
        };

        // Phase 2: durable publication (temp -> fsync -> rename -> fsync
        // dir). Any I/O failure leaves no file behind and the transaction
        // stays open, so the caller can retry or abort.
        let (path, file_size) = {
            let g = self.inner.lock().unwrap();
            (
                g.root.join(format::segment_name(version)),
                body.len() as u64,
            )
        };
        self.inner.lock().unwrap().vfs.write_atomic(&path, &body)?;

        // Phase 3: publish into the in-memory index atomically with the
        // version counter.
        let mut g = self.inner.lock().unwrap();
        // Another commit on the same engine cannot race (mutex), but defend
        // the invariant explicitly.
        assert_eq!(g.next_version, version, "version allocated out of order");
        for c in cells {
            apply_cell(
                &mut g.index,
                c.key,
                Entry {
                    version,
                    val: c.val,
                },
            );
        }
        g.files.insert(
            version,
            FileMeta {
                is_base: false,
                size: file_size,
                stale: false,
            },
        );
        g.next_version = version + 1;
        g.txs.remove(&tx);
        Ok(version)
    }

    /// Current committed version.
    pub fn current_version(&self) -> u64 {
        self.inner.lock().unwrap().next_version - 1
    }

    /// Snapshot pins, oldest first.
    pub fn list_snapshots(&self) -> Vec<SnapshotInfo> {
        let g = self.inner.lock().unwrap();
        g.snapshots.values().cloned().collect()
    }

    /// Compaction horizon: min read version over all active txs/snapshots.
    fn horizon(g: &Inner) -> u64 {
        let latest = g.next_version - 1;
        let mut h = latest;
        for t in g.txs.values() {
            h = h.min(t.read_version);
        }
        for s in g.snapshots.values() {
            h = h.min(s.version);
        }
        h
    }

    /// Run one reclamation pass. See module docs for the invariant.
    pub fn gc(&self) -> Result<GcReport> {
        let mut g = self.inner.lock().unwrap();

        let horizon = Self::horizon(&g);
        let mut report = GcReport {
            horizon,
            base_version_before: g.base_version,
            base_written: false,
            base_cells: 0,
            files_removed: 0,
            bytes_reclaimed: 0,
            stale_left: 0,
            noop_reason: None,
        };

        if horizon <= g.base_version {
            report.noop_reason = Some("horizon already covered by newest base");
            // Even with nothing new to compact, a previous pass may have
            // left obsolete files whose unlink failed; retry them now.
            retry_stale_removals(&mut g, &mut report);
            return Ok(report);
        }

        // Build the base: for every key, its newest entry visible at H.
        // Tombstones are dropped — the files containing the key's older
        // versions are all removed with this pass, so absence is identical
        // to a tombstone for every snapshot that can still be read.
        let mut base_cells: Vec<Cell> = Vec::new();
        let mut prune: Vec<(Vec<u8>, Entry)> = Vec::new();
        for (key, entries) in &g.index {
            let mut keep: Option<&Entry> = None;
            for e in entries {
                if e.version <= horizon {
                    keep = Some(e);
                } else {
                    break;
                }
            }
            if let Some(e) = keep {
                if e.val.is_some() {
                    base_cells.push(Cell {
                        key: key.clone(),
                        val: e.val.clone(),
                        version: e.version,
                    });
                }
                prune.push((key.clone(), e.clone()));
            }
        }

        // Publish the base before deleting anything.
        let body = format::encode_base(horizon, &mut base_cells);
        let base_path = g.root.join(format::base_name(horizon));
        let base_size = body.len() as u64;
        g.vfs.write_atomic(&base_path, &body)?;

        // Now mark every file <= H obsolete and unlink it. A failed unlink
        // leaves a harmless stale file retried later; the base already
        // supersedes it, so this never blocks future commits.
        let obsolete: Vec<(u64, FileMeta)> = g
            .files
            .iter()
            .filter(|(v, _)| **v <= horizon)
            .map(|(v, m)| (*v, m.clone()))
            .collect();

        remove_files(&mut g, &obsolete, &mut report);

        g.files.insert(
            horizon,
            FileMeta {
                is_base: true,
                size: base_size,
                stale: false,
            },
        );
        g.base_version = horizon;

        // Prune in-memory history: all readers that remain read at >= H, so
        // only the newest entry <= H per key must survive alongside entries
        // newer than H.
        for (key, keep) in prune {
            if let Some(entries) = g.index.get_mut(&key) {
                entries.retain(|e| e.version > horizon);
                entries.insert(0, keep);
            }
        }

        report.base_written = true;
        report.base_cells = base_cells.len();
        Ok(report)
    }

    /// Snapshot of counters for observability/tests.
    pub fn stats(&self) -> Stats {
        let g = self.inner.lock().unwrap();
        let mut open_transactions: Vec<(u64, u64)> =
            g.txs.iter().map(|(id, t)| (*id, t.read_version)).collect();
        open_transactions.sort();
        let mut snapshots: Vec<SnapshotInfo> = g.snapshots.values().cloned().collect();
        snapshots.sort_by_key(|s| s.id);
        Stats {
            current_version: g.next_version - 1,
            base_version: g.base_version,
            keys: g.index.len(),
            live_cells: g.index.values().map(|v| v.len()).sum(),
            open_transactions,
            snapshots,
            segment_files: g.files.values().filter(|m| !m.is_base && !m.stale).count(),
            base_files: g.files.values().filter(|m| m.is_base && !m.stale).count(),
            stale_files: g.files.values().filter(|m| m.stale).count(),
            live_bytes: g.files.values().filter(|m| !m.stale).map(|m| m.size).sum(),
            stale_bytes: g.files.values().filter(|m| m.stale).map(|m| m.size).sum(),
        }
    }

    /// Files currently considered live on disk (version, is_base, size).
    pub fn live_files(&self) -> Vec<(u64, bool, u64)> {
        let g = self.inner.lock().unwrap();
        g.files
            .iter()
            .filter(|(_, m)| !m.stale)
            .map(|(v, m)| (*v, m.is_base, m.size))
            .collect()
    }
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

impl Inner {
    fn tx_mut(&mut self, tx: u64) -> Result<&mut TxState> {
        self.txs.get_mut(&tx).ok_or(Error::UnknownTx(tx))
    }
}

fn file_name(v: u64, is_base: bool) -> String {
    if is_base {
        format::base_name(v)
    } else {
        format::segment_name(v)
    }
}

/// Unlink the given files. Files whose unlink fails stay in the map marked
/// `stale`, counted in `report.stale_left`, and are retried by a later pass.
fn remove_files(g: &mut Inner, targets: &[(u64, FileMeta)], report: &mut GcReport) {
    for (v, meta) in targets {
        let path = g.root.join(file_name(*v, meta.is_base));
        match g.vfs.remove_file(&path) {
            Ok(()) => {
                g.files.remove(v);
                report.files_removed += 1;
                report.bytes_reclaimed += meta.size;
            }
            Err(_) => {
                if let Some(m) = g.files.get_mut(v) {
                    m.stale = true;
                }
                report.stale_left += 1;
            }
        }
    }
}

/// Retry unlink of files an earlier compaction already superseded.
fn retry_stale_removals(g: &mut Inner, report: &mut GcReport) {
    let stale: Vec<(u64, FileMeta)> = g
        .files
        .iter()
        .filter(|(_, m)| m.stale)
        .map(|(v, m)| (*v, m.clone()))
        .collect();
    remove_files(g, &stale, report);
}

fn apply_cell(index: &mut BTreeMap<Vec<u8>, Vec<Entry>>, key: Vec<u8>, e: Entry) {
    let entries = index.entry(key).or_default();
    // Replay receives files in version order; guard against duplicates
    // anyway so the binary-search read stays valid.
    match entries.binary_search_by_key(&e.version, |x| x.version) {
        Ok(pos) => entries[pos] = e,
        Err(pos) => entries.insert(pos, e),
    }
}

fn read_at(index: &BTreeMap<Vec<u8>, Vec<Entry>>, key: &[u8], version: u64) -> Option<Vec<u8>> {
    let entries = index.get(key)?;
    let mut found: Option<&Entry> = None;
    for e in entries {
        if e.version <= version {
            found = Some(e);
        } else {
            break;
        }
    }
    found.and_then(|e| e.val.clone())
}
