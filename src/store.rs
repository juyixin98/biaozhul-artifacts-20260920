//! File-backed content-addressable block store.
//!
//! ## Disk format (version 1)
//!
//! ```text
//! <data-dir>/
//!   VERSION                       # contains "cas-repo v1\n"
//!   blocks/
//!     <aa>/                       # first byte of the digest, 256 shards
//!       <rest-62-hex>             # immutable blob: exact block bytes
//!       <rest-62-hex>.refs.json   # sidecar: {"version":1,"refs":["<sha256>", ...]}
//!       .<...>.tmp-<rand>         # staging files (atomic rename source)
//!   roots/
//!     <name>.json                 # {"hash":"<sha256>","version":N,"updated_at_ms":T}
//!     .<name>.json.tmp-<rand>     # staging files
//! ```
//!
//! Invariants:
//! * Blob bytes always hash to their path digest. Hash is verified on read and
//!   during GC marking; it is an integrity check only, never an access token.
//! * Blobs are immutable: a `put` of an existing digest never rewrites the
//!   blob, only unions the declared reference set.
//! * A root pointer appears via one temp-file + fsync + rename step, so root
//!   updates are atomic: readers see either the old or the new version.
//!
//! ## Synchronization boundary
//!
//! One store object owns one data directory (single process). Internally:
//! * `upload` serializes puts of the *same* staging paths;
//! * a `RwLock` (`roots_lock`) separates **writers** from **GC**: `put_block`
//!   and `put_root` hold it in read/write mode respectively, GC holds it in
//!   write mode for the whole mark+sweep. Uploads and root switches therefore
//!   never overlap a GC cycle, which closes the "reference added while GC
//!   marks" and "root switched while GC sweeps" windows by construction.
//! * the pin table counts in-flight readers and GC-live blocks. `get_block`
//!   takes no lock: it pins, reads, verifies, unpins. Sweep checks the pin
//!   count and deletes while holding the pin lock, closing the reader/GC
//!   window.

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};

use crate::hash::Sha256;
use crate::json::Json;
use crate::vfs::{Vfs, VfsError};

const FORMAT_VERSION_LINE: &str = "cas-repo v1\n";
/// Hard cap enforced by the store itself; the HTTP layer uses the same value.
pub const DEFAULT_MAX_BLOCK_BYTES: usize = 32 * 1024 * 1024;

// ---------------- errors ----------------

#[derive(Debug)]
pub enum StoreError {
    Vfs(VfsError),
    NotFound(String),
    Corrupt {
        hash: String,
        detail: String,
    },
    HashMismatch {
        declared: String,
        actual: String,
    },
    InvalidRootName(String),
    InvalidHash(String),
    VersionConflict {
        name: String,
        expected: u64,
        actual: u64,
    },
    TooLarge {
        size: usize,
        limit: usize,
    },
    BadMetadata {
        context: String,
        detail: String,
    },
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Vfs(e) => write!(f, "{e}"),
            StoreError::NotFound(s) => write!(f, "not found: {s}"),
            StoreError::Corrupt { hash, detail } => {
                write!(f, "corrupt block {hash}: {detail}")
            }
            StoreError::HashMismatch { declared, actual } => write!(
                f,
                "hash mismatch: declared {declared}, actual sha256 {actual}"
            ),
            StoreError::InvalidRootName(n) => write!(
                f,
                "invalid root name {n:?}: allowed [A-Za-z0-9._-], 1..=128 chars, no leading dot"
            ),
            StoreError::InvalidHash(h) => write!(f, "invalid sha256 digest: {h}"),
            StoreError::VersionConflict {
                name,
                expected,
                actual,
            } => write!(
                f,
                "root {name} version conflict: expected {expected}, current {actual}"
            ),
            StoreError::TooLarge { size, limit } => {
                write!(f, "block too large: {size} bytes > limit {limit}")
            }
            StoreError::BadMetadata { context, detail } => {
                write!(f, "bad metadata for {context}: {detail}")
            }
        }
    }
}

impl std::error::Error for StoreError {}

impl From<VfsError> for StoreError {
    fn from(e: VfsError) -> Self {
        StoreError::Vfs(e)
    }
}

type StoreResult<T> = Result<T, StoreError>;

pub fn parse_hash(h: &str) -> StoreResult<Sha256> {
    Sha256::from_hex(h).ok_or_else(|| StoreError::InvalidHash(h.to_string()))
}

// ---------------- paths & names ----------------

fn validate_root_name(name: &str) -> StoreResult<()> {
    let ok = !name.is_empty()
        && name.len() <= 128
        && !name.starts_with('.')
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'));
    if ok {
        Ok(())
    } else {
        Err(StoreError::InvalidRootName(name.to_string()))
    }
}

fn block_blob_rel(h: &Sha256) -> PathBuf {
    let hex = h.to_hex();
    PathBuf::from("blocks").join(&hex[..2]).join(&hex[2..])
}

fn block_refs_rel(h: &Sha256) -> PathBuf {
    let hex = h.to_hex();
    PathBuf::from("blocks")
        .join(&hex[..2])
        .join(format!("{}.refs.json", &hex[2..]))
}

fn root_rel(name: &str) -> PathBuf {
    PathBuf::from("roots").join(format!("{name}.json"))
}

// ---------------- public result types ----------------

#[derive(Debug, Clone)]
pub struct PutOutcome {
    pub hash: Sha256,
    /// True when the blob already existed (content de-duplication).
    pub deduplicated: bool,
    /// Number of distinct references stored after union with the old sidecar.
    pub refs_count: usize,
}

#[derive(Debug, Clone)]
pub struct RootRecord {
    pub hash: Sha256,
    pub version: u64,
    pub updated_at_ms: u64,
}

#[derive(Debug, Clone, Default)]
pub struct MissingReference {
    /// Parent digest; `None` when the missing block is a root target.
    pub parent: Option<String>,
    pub missing: String,
}

#[derive(Debug, Clone, Default)]
pub struct GcReport {
    pub roots_scanned: usize,
    pub blocks_before: usize,
    pub blocks_live: usize,
    pub blocks_removed: usize,
    pub bytes_removed: u64,
    pub removed: Vec<String>,
    pub missing_references: Vec<MissingReference>,
    pub corrupt_blocks: Vec<String>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Default)]
pub struct StoreStats {
    pub roots: usize,
    pub blocks: usize,
    pub block_bytes: u64,
}

// ---------------- pin table ----------------

/// Reference-count pins that protect a digest from garbage collection.
///
/// Holders: in-flight gets (1 pin for the read duration), uploads (1 pin from
/// before the existence check until the blob is durable), and GC itself (1 pin
/// per reachable block for the whole sweep).
pub struct PinTable {
    counts: Mutex<HashMap<Sha256, usize>>,
}

impl PinTable {
    fn new() -> Self {
        PinTable {
            counts: Mutex::new(HashMap::new()),
        }
    }

    fn pin(&self, h: Sha256) {
        *self.counts.lock().unwrap().entry(h).or_insert(0) += 1;
    }

    fn unpin(&self, h: Sha256) {
        let mut g = self.counts.lock().unwrap();
        match g.get_mut(&h) {
            Some(n) if *n > 1 => *n -= 1,
            Some(_) => {
                g.remove(&h);
            }
            None => debug_assert!(false, "unpin without pin"),
        }
    }

    /// Whether any holder currently protects this digest (diagnostics/tests).
    pub fn is_pinned(&self, h: &Sha256) -> bool {
        self.counts.lock().unwrap().contains_key(h)
    }
}

/// Public RAII pin returned by [`Store::pin_block`]: while alive, garbage
/// collection will not delete the block even if it is unreachable. Drop to
/// release. This is what backs "concurrent read protects a live block".
pub struct PinGuard {
    table: Arc<PinTable>,
    hash: Sha256,
}

impl Drop for PinGuard {
    fn drop(&mut self) {
        self.table.unpin(self.hash);
    }
}

/// Internal short-lived RAII pin.
struct Pin<'a> {
    table: &'a PinTable,
    hash: Sha256,
    armed: bool,
}

impl<'a> Pin<'a> {
    fn new(table: &'a PinTable, hash: Sha256) -> Self {
        table.pin(hash);
        Pin {
            table,
            hash,
            armed: true,
        }
    }

    fn disarm(mut self) {
        self.armed = false;
        self.table.unpin(self.hash);
    }
}

impl Drop for Pin<'_> {
    fn drop(&mut self) {
        if self.armed {
            self.table.unpin(self.hash);
        }
    }
}

// ---------------- store ----------------

pub struct Store<V: Vfs = Box<dyn Vfs>> {
    root: PathBuf,
    vfs: V,
    pins: Arc<PinTable>,
    /// Serializes blob/sidecar publishing so two identical puts cannot race
    /// each other's temp files.
    upload: Mutex<()>,
    /// Writer lock taken for the whole mark+sweep. `put_block` and `get_root`
    /// take a read lock, `put_root`/`delete_root` a write lock: uploads and
    /// reads proceed concurrently with each other but never with GC.
    roots_lock: RwLock<()>,
    max_block_bytes: usize,
}

pub type DynStore = Store<Box<dyn Vfs>>;

impl DynStore {
    pub fn open_boxed(data_dir: impl Into<PathBuf>, vfs: Box<dyn Vfs>) -> StoreResult<Arc<Self>> {
        Self::open(data_dir, vfs)
    }
}

impl<V: Vfs> Store<V> {
    pub fn open(data_dir: impl Into<PathBuf>, vfs: V) -> StoreResult<Arc<Self>> {
        let root: PathBuf = data_dir.into();
        vfs.create_dir_all(&root, "mkdir_data")?;
        vfs.create_dir_all(&root.join("blocks"), "mkdir_blocks")?;
        vfs.create_dir_all(&root.join("roots"), "mkdir_roots")?;

        let version_path = root.join("VERSION");
        match vfs.read(&version_path, "read_version") {
            Ok(bytes) => {
                if bytes != FORMAT_VERSION_LINE.as_bytes() {
                    return Err(StoreError::BadMetadata {
                        context: "VERSION".into(),
                        detail: format!(
                            "unexpected contents: {:?}",
                            String::from_utf8_lossy(&bytes)
                        ),
                    });
                }
            }
            Err(e) if e.is_not_found() => {
                vfs.write_atomic(
                    &version_path,
                    FORMAT_VERSION_LINE.as_bytes(),
                    "rename_version",
                )?;
            }
            Err(e) => return Err(e.into()),
        }

        let store = Arc::new(Store {
            root,
            vfs,
            pins: Arc::new(PinTable::new()),
            upload: Mutex::new(()),
            roots_lock: RwLock::new(()),
            max_block_bytes: DEFAULT_MAX_BLOCK_BYTES,
        });
        store.cleanup_temp_files()?;
        Ok(store)
    }

    pub fn data_dir(&self) -> &Path {
        &self.root
    }

    fn p(&self, rel: &Path) -> PathBuf {
        self.root.join(rel)
    }

    /// Remove leftover staging files from interrupted writes (best effort).
    fn cleanup_temp_files(&self) -> StoreResult<()> {
        for entry in self
            .vfs
            .list_dir(&self.p(Path::new("roots")), "list_roots")?
        {
            if entry.starts_with('.') {
                let _ = self
                    .vfs
                    .remove_file(&self.p(Path::new("roots")).join(&entry), "cleanup_temp");
            }
        }
        for shard in self
            .vfs
            .list_dir(&self.p(Path::new("blocks")), "list_blocks")?
        {
            if shard.len() != 2 || !shard.bytes().all(|b| b.is_ascii_hexdigit()) {
                continue;
            }
            let shard_path = self.p(Path::new("blocks")).join(&shard);
            for entry in self.vfs.list_dir(&shard_path, "list_shard")? {
                if entry.starts_with('.') {
                    let _ = self
                        .vfs
                        .remove_file(&shard_path.join(&entry), "cleanup_temp");
                }
            }
        }
        Ok(())
    }

    // ---------- blocks ----------

    /// Store `data`. If `declared` is given it must match the actual SHA-256.
    /// `refs` declares outgoing references (union with any prior sidecar);
    /// references may point at blocks not yet uploaded (forward references).
    pub fn put_block(
        &self,
        data: &[u8],
        declared: Option<Sha256>,
        refs: &[Sha256],
    ) -> StoreResult<PutOutcome> {
        if data.len() > self.max_block_bytes {
            return Err(StoreError::TooLarge {
                size: data.len(),
                limit: self.max_block_bytes,
            });
        }
        let hash = Sha256::hash(data);
        if let Some(d) = declared {
            if d != hash {
                return Err(StoreError::HashMismatch {
                    declared: d.to_hex(),
                    actual: hash.to_hex(),
                });
            }
        }

        let _g = self.upload.lock().unwrap();
        // Uploads are mutually exclusive with GC. Reading the old sidecar and
        // publishing the new one can therefore never race the mark phase, so
        // a reference added by this put is always visible to a GC that runs
        // afterwards, and no GC is running now.
        let _rg = self.roots_lock.read().unwrap();
        // Pin *before* the existence check: even if a future GC ever ran here
        // (it cannot — read lock), the defense-in-depth ordering is pin then
        // check, never the reverse.
        let _pin = Pin::new(&self.pins, hash);

        let blob_path = self.p(&block_blob_rel(&hash));
        let refs_path = self.p(&block_refs_rel(&hash));
        let existed = self.vfs.exists(&blob_path, "exists_block")?;

        if !existed {
            self.vfs
                .create_dir_all(blob_path.parent().unwrap(), "mkdir_shard")?;
            self.vfs.write_atomic(&blob_path, data, "rename_block")?;
        }

        let mut all: HashSet<Sha256> = match self.vfs.read(&refs_path, "read_refs") {
            Ok(bytes) => parse_refs_sidecar(&bytes, &hash.to_hex())?
                .into_iter()
                .collect(),
            Err(e) if e.is_not_found() => HashSet::new(),
            Err(e) => return Err(e.into()),
        };
        let before = all.len();
        all.extend(refs.iter().copied());

        // Rewrite the sidecar whenever refs changed or when writing a fresh
        // blob that was given refs.
        if all.len() != before || (!existed && !all.is_empty()) {
            let doc = build_refs_sidecar(&all);
            self.vfs
                .write_atomic(&refs_path, doc.as_bytes(), "rename_block_refs")?;
        }

        Ok(PutOutcome {
            hash,
            deduplicated: existed,
            refs_count: all.len(),
        })
    }

    /// Read a block, holding a GC pin for the duration and verifying the
    /// digest of the bytes on disk.
    pub fn get_block(&self, hash: &Sha256) -> StoreResult<Vec<u8>> {
        let _pin = Pin::new(&self.pins, *hash);
        let path = self.p(&block_blob_rel(hash));
        let data = match self.vfs.read(&path, "read_block") {
            Ok(d) => d,
            Err(e) if e.is_not_found() => {
                return Err(StoreError::NotFound(format!("block {}", hash.to_hex())))
            }
            Err(e) => return Err(e.into()),
        };
        // Integrity verification: the digest is the content identity. A
        // mismatch means bit rot or out-of-band tampering, never "a different
        // but acceptable block".
        if Sha256::hash(&data) != *hash {
            return Err(StoreError::Corrupt {
                hash: hash.to_hex(),
                detail: "stored bytes do not hash to their address".into(),
            });
        }
        Ok(data)
    }

    pub fn block_exists(&self, hash: &Sha256) -> StoreResult<bool> {
        Ok(self
            .vfs
            .exists(&self.p(&block_blob_rel(hash)), "exists_block")?)
    }

    pub fn block_refs(&self, hash: &Sha256) -> StoreResult<Vec<Sha256>> {
        match self.vfs.read(&self.p(&block_refs_rel(hash)), "read_refs") {
            Ok(bytes) => parse_refs_sidecar(&bytes, &hash.to_hex()),
            Err(e) if e.is_not_found() => Ok(Vec::new()),
            Err(e) => Err(e.into()),
        }
    }

    pub fn block_info(&self, hash: &Sha256) -> StoreResult<Json> {
        let present = self.block_exists(hash)?;
        let refs = self.block_refs(hash)?;
        let mut obj = std::collections::BTreeMap::new();
        obj.insert("hash".into(), Json::str(hash.to_hex()));
        obj.insert("present".into(), Json::Bool(present));
        obj.insert(
            "refs".into(),
            Json::Arr(refs.iter().map(|r| Json::str(r.to_hex())).collect()),
        );
        Ok(Json::Obj(obj))
    }

    /// Pin a block against garbage collection for the guard's lifetime.
    ///
    /// Returns `None` when the blob is not on disk. Use this to hold a block
    /// across a slow read or while performing a multi-step operation; the pin
    /// is the same primitive `get_block` and GC use internally.
    pub fn pin_block(&self, hash: &Sha256) -> StoreResult<Option<PinGuard>> {
        if !self.block_exists(hash)? {
            return Ok(None);
        }
        self.pins.pin(*hash);
        Ok(Some(PinGuard {
            table: self.pins.clone(),
            hash: *hash,
        }))
    }

    /// Whether `hash` currently has any protecting pin (test/diagnostic hook).
    pub fn is_pinned(&self, hash: &Sha256) -> bool {
        self.pins.is_pinned(hash)
    }

    // ---------- roots ----------

    /// Publish a root pointer atomically. `expected_version` implements
    /// optimistic concurrency: the update succeeds only when the on-disk
    /// version equals it (a missing root counts as version 0).
    pub fn put_root(
        &self,
        name: &str,
        hash: Sha256,
        expected_version: Option<u64>,
    ) -> StoreResult<RootRecord> {
        validate_root_name(name)?;
        let _g = self.roots_lock.write().unwrap();

        // The target blob must exist. Pin it until the pointer is published so
        // GC (which cannot run concurrently — same write lock — but defense in
        // depth) cannot remove the block between check and publish.
        let _pin = Pin::new(&self.pins, hash);
        if !self
            .vfs
            .exists(&self.p(&block_blob_rel(&hash)), "exists_block")?
        {
            return Err(StoreError::NotFound(format!(
                "block {} referenced by new root {name}",
                hash.to_hex()
            )));
        }

        let path = self.p(&root_rel(name));
        let current = self.read_root_locked(name)?;
        let next_version = current.as_ref().map(|r| r.version + 1).unwrap_or(1);
        if let Some(expected) = expected_version {
            let actual = current.as_ref().map(|r| r.version).unwrap_or(0);
            if expected != actual {
                return Err(StoreError::VersionConflict {
                    name: name.to_string(),
                    expected,
                    actual,
                });
            }
        }

        let rec = RootRecord {
            hash,
            version: next_version,
            updated_at_ms: now_ms(),
        };
        let doc = build_root_doc(&rec);
        // Single atomic publish step. If this call fails (including an
        // injected fault), no root file is created/modified: the old pointer
        // stays readable and the new block is simply unreachable.
        self.vfs
            .write_atomic(&path, doc.as_bytes(), "rename_root")?;
        Ok(rec)
    }

    pub fn get_root(&self, name: &str) -> StoreResult<RootRecord> {
        validate_root_name(name)?;
        let _g = self.roots_lock.read().unwrap();
        match self.read_root_locked(name)? {
            Some(r) => Ok(r),
            None => Err(StoreError::NotFound(format!("root {name}"))),
        }
    }

    pub fn list_roots(&self) -> StoreResult<Vec<(String, RootRecord)>> {
        let _g = self.roots_lock.read().unwrap();
        self.list_roots_locked()
    }

    pub fn delete_root(&self, name: &str) -> StoreResult<bool> {
        validate_root_name(name)?;
        let _g = self.roots_lock.write().unwrap();
        let path = self.p(&root_rel(name));
        match self.vfs.remove_file(&path, "delete_root") {
            Ok(()) => Ok(true),
            Err(e) if e.is_not_found() => Ok(false),
            Err(e) => Err(e.into()),
        }
    }

    fn read_root_locked(&self, name: &str) -> StoreResult<Option<RootRecord>> {
        let path = self.p(&root_rel(name));
        match self.vfs.read(&path, "read_root") {
            Ok(bytes) => Ok(Some(parse_root_doc(&bytes, name)?)),
            Err(e) if e.is_not_found() => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    fn list_roots_locked(&self) -> StoreResult<Vec<(String, RootRecord)>> {
        let mut out = Vec::new();
        for entry in self
            .vfs
            .list_dir(&self.p(Path::new("roots")), "list_roots")?
        {
            let Some(name) = entry.strip_suffix(".json") else {
                continue;
            };
            if name.starts_with('.') {
                continue;
            }
            if let Ok(Some(rec)) = self.read_root_locked(name) {
                out.push((name.to_string(), rec));
            }
        }
        out.sort_by(|a, b| a.0.cmp(&b.0));
        Ok(out)
    }

    // ---------- GC ----------

    /// Mark all blocks reachable from named roots through reference sidecars,
    /// then sweep every unpinned blob. Root switches are blocked for the whole
    /// operation; gets/uploads continue to work and their pins protect blocks.
    pub fn gc(&self) -> StoreResult<GcReport> {
        // Write lock: no root pointer may change during marking or sweeping.
        let _g = self.roots_lock.write().unwrap();

        let mut report = GcReport::default();

        // ---- mark ----
        let mut marked: HashSet<Sha256> = HashSet::new();
        let mut queue: Vec<(Option<Sha256>, Sha256)> = Vec::new(); // (parent, child)

        let roots = self.list_roots_locked()?;
        report.roots_scanned = roots.len();
        for (name, rec) in roots {
            queue.push((None, rec.hash));
            let _ = name;
        }

        while let Some((parent, hash)) = queue.pop() {
            if marked.contains(&hash) {
                continue;
            }
            // Pin immediately; un-pinned only after the sweep finishes.
            let pin = Pin::new(&self.pins, hash);
            let blob = self.p(&block_blob_rel(&hash));
            let exists = match self.vfs.exists(&blob, "exists_block") {
                Ok(b) => b,
                Err(e) => {
                    report
                        .warnings
                        .push(format!("cannot stat {}: {e}", hash.to_hex()));
                    pin.disarm();
                    continue;
                }
            };
            if !exists {
                report.missing_references.push(MissingReference {
                    parent: parent.map(|p| p.to_hex()),
                    missing: hash.to_hex(),
                });
                pin.disarm();
                continue;
            }

            // Verify integrity of every reachable blob.
            if let Ok(bytes) = self.vfs.read(&blob, "read_block") {
                if Sha256::hash(&bytes) != hash {
                    report.corrupt_blocks.push(hash.to_hex());
                }
            } else {
                report
                    .warnings
                    .push(format!("cannot read {}", hash.to_hex()));
            }

            marked.insert(hash);
            pin.into_kept(); // bulk-unpin after sweep

            for child in self.block_refs(&hash).unwrap_or_default() {
                if !marked.contains(&child) {
                    queue.push((Some(hash), child));
                }
            }
        }
        report.blocks_live = marked.len();

        // ---- sweep ----
        let blocks_dir = self.p(Path::new("blocks"));
        let shards = self.vfs.list_dir(&blocks_dir, "list_blocks")?;
        let mut blocks_seen = 0usize;
        for shard in shards {
            if shard.len() != 2 || !shard.bytes().all(|b| b.is_ascii_hexdigit()) {
                continue;
            }
            let shard_path = blocks_dir.join(&shard);
            for entry in self.vfs.list_dir(&shard_path, "list_shard")? {
                // Staging files belong to an in-progress put (never deleted
                // here — that would corrupt a concurrent upload) or to a
                // crashed write (reclaimed at next store open).
                if entry.starts_with('.') {
                    continue;
                }
                if let Some(rest) = entry.strip_suffix(".refs.json") {
                    if rest.len() != 62 || !rest.bytes().all(|b| b.is_ascii_hexdigit()) {
                        report
                            .warnings
                            .push(format!("ignoring unexpected file blocks/{shard}/{entry}"));
                    }
                    // Sidecars are removed together with their blob in sweep_blob.
                    continue;
                }
                if entry.len() != 62 || !entry.bytes().all(|b| b.is_ascii_hexdigit()) {
                    report
                        .warnings
                        .push(format!("ignoring unexpected file blocks/{shard}/{entry}"));
                    continue;
                }
                blocks_seen += 1;
                self.sweep_blob(&shard, &entry, &marked, &mut report)?;
            }
        }

        report.blocks_before = blocks_seen;

        // Release the bulk mark pins.
        for h in &marked {
            self.pins.unpin(*h);
        }
        Ok(report)
    }

    /// Remove one unreachable blob (and its sidecar). The pin-table lock is
    /// held across the actual unlink: a concurrent reader/uploader must either
    /// have its pin visible (skip deletion) or pin *after* the unlink
    /// completed (and then observe "not found" / re-create the blob).
    fn sweep_blob(
        &self,
        shard: &str,
        rest: &str,
        marked: &HashSet<Sha256>,
        report: &mut GcReport,
    ) -> StoreResult<()> {
        let hex = format!("{shard}{rest}");
        let hash = match parse_hash(&hex) {
            Ok(h) => h,
            Err(_) => {
                report
                    .warnings
                    .push(format!("ignoring non-hash blob blocks/{shard}/{rest}"));
                return Ok(());
            }
        };
        if marked.contains(&hash) {
            return Ok(());
        }
        let blob_path = self.p(Path::new("blocks")).join(shard).join(rest);
        let refs_path = self
            .p(Path::new("blocks"))
            .join(shard)
            .join(format!("{rest}.refs.json"));

        let counts = self.pins.counts.lock().unwrap();
        if counts.contains_key(&hash) {
            // Pinned by an in-flight get or an upload in progress: reachable
            // or not, it is protected this round.
            return Ok(());
        }
        let size = self
            .vfs
            .read(&blob_path, "read_block")
            .map(|b| b.len() as u64)
            .unwrap_or(0);
        self.vfs.remove_file(&blob_path, "remove_block")?;
        if let Err(e) = self.vfs.remove_file(&refs_path, "remove_refs") {
            if !e.is_not_found() {
                return Err(e.into());
            }
        }
        drop(counts);
        report.blocks_removed += 1;
        report.bytes_removed += size;
        report.removed.push(hex);
        Ok(())
    }

    // ---------- stats ----------

    pub fn stats(&self) -> StoreResult<StoreStats> {
        let mut stats = StoreStats {
            roots: self.list_roots_locked()?.len(),
            ..Default::default()
        };
        let blocks_dir = self.p(Path::new("blocks"));
        for shard in self.vfs.list_dir(&blocks_dir, "list_blocks")? {
            if shard.len() != 2 || !shard.bytes().all(|b| b.is_ascii_hexdigit()) {
                continue;
            }
            for entry in self.vfs.list_dir(&blocks_dir.join(&shard), "list_shard")? {
                if entry.len() == 62 && entry.bytes().all(|b| b.is_ascii_hexdigit()) {
                    stats.blocks += 1;
                    if let Ok(bytes) = self
                        .vfs
                        .read(&blocks_dir.join(&shard).join(&entry), "read_block")
                    {
                        stats.block_bytes += bytes.len() as u64;
                    }
                }
            }
        }
        Ok(stats)
    }
}

// RAII helper: GC marks a pin that it releases in bulk after the sweep.
impl<'a> Pin<'a> {
    /// Release ownership of the pin to the caller: the pin stays armed in the
    /// table and must be un-pinned explicitly once the sweep is over.
    fn into_kept(mut self) {
        self.armed = false;
    }
}

// ---------------- metadata codecs ----------------

fn build_refs_sidecar(refs: &HashSet<Sha256>) -> String {
    let mut v: Vec<String> = refs.iter().map(|h| h.to_hex()).collect();
    v.sort();
    let arr = Json::Arr(v.into_iter().map(Json::str).collect());
    let mut obj = std::collections::BTreeMap::new();
    obj.insert("version".into(), Json::from_u64(1));
    obj.insert("refs".into(), arr);
    Json::Obj(obj).to_compact_string()
}

fn parse_refs_sidecar(bytes: &[u8], context_hash: &str) -> StoreResult<Vec<Sha256>> {
    let text = std::str::from_utf8(bytes).map_err(|_| StoreError::BadMetadata {
        context: format!("refs of {context_hash}"),
        detail: "not utf-8".into(),
    })?;
    let doc = crate::json::parse(text).map_err(|e| StoreError::BadMetadata {
        context: format!("refs of {context_hash}"),
        detail: e.to_string(),
    })?;
    let arr = doc
        .get("refs")
        .and_then(Json::as_array)
        .ok_or_else(|| StoreError::BadMetadata {
            context: format!("refs of {context_hash}"),
            detail: "missing \"refs\" array".into(),
        })?;
    let mut out = Vec::new();
    for item in arr {
        let s = item.as_str().ok_or_else(|| StoreError::BadMetadata {
            context: format!("refs of {context_hash}"),
            detail: "ref entry is not a string".into(),
        })?;
        out.push(parse_hash(s)?);
    }
    Ok(out)
}

fn build_root_doc(rec: &RootRecord) -> String {
    let mut obj = std::collections::BTreeMap::new();
    obj.insert("hash".into(), Json::str(rec.hash.to_hex()));
    obj.insert("version".into(), Json::from_u64(rec.version));
    obj.insert("updated_at_ms".into(), Json::from_u64(rec.updated_at_ms));
    Json::Obj(obj).to_compact_string()
}

fn parse_root_doc(bytes: &[u8], name: &str) -> StoreResult<RootRecord> {
    let text = std::str::from_utf8(bytes).map_err(|_| StoreError::BadMetadata {
        context: format!("root {name}"),
        detail: "not utf-8".into(),
    })?;
    let doc = crate::json::parse(text).map_err(|e| StoreError::BadMetadata {
        context: format!("root {name}"),
        detail: e.to_string(),
    })?;
    let hash = parse_hash(doc.get("hash").and_then(Json::as_str).ok_or_else(|| {
        StoreError::BadMetadata {
            context: format!("root {name}"),
            detail: "missing hash".into(),
        }
    })?)?;
    let version =
        doc.get("version")
            .and_then(Json::as_u64)
            .ok_or_else(|| StoreError::BadMetadata {
                context: format!("root {name}"),
                detail: "missing version".into(),
            })?;
    let updated_at_ms = doc.get("updated_at_ms").and_then(Json::as_u64).unwrap_or(0);
    Ok(RootRecord {
        hash,
        version,
        updated_at_ms,
    })
}

fn now_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}
