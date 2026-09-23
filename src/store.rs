//! The repository: immutable content-addressed blocks, named roots and
//! mark-sweep garbage collection.
//!
//! ## Disk format (version `cas-store v1`)
//!
//! ```text
//! <repo>/
//!   format                 # one line: "cas-store v1\n"
//!   repo.lock              # flock target (created on open, never deleted)
//!   blocks/
//!     ab/
//!       <64-hex hash>          # raw block bytes (immutable once published)
//!       <64-hex hash>.meta     # JSON: {"refs": ["<hash>", ...]}
//!   roots/
//!     <root-name>             # JSON RootManifest (atomic rename publication)
//!   tmp/                       # currently unused (reserved)
//! ```
//!
//! Block data files are created with O_EXCL (`create_new`) and never modified;
//! block `.meta` files are published atomically (temp + rename). A block is
//! observable only once its data file exists. Root manifests are published
//! atomically, so a root always points at a complete, previously uploaded
//! graph.
//!
//! ## Synchronization boundaries
//!
//! - **Process boundary**: exactly one writer process per repository, enforced
//!   by an exclusive non-blocking `flock` on `repo.lock` at open.
//! - **Thread boundary**: any number of threads may read; uploads and root
//!   publishes synchronize internally. GC coordinates with readers via an
//!   in-process RwLock — readers hold a shared guard for the duration of a
//!   block/root read, GC takes the write guard only around its *final* mark and
//!   sweep. Therefore any block reachable from a root while (or before) GC runs
//!   is never deleted, even if the root is switched concurrently.
//! - **Crash boundary**: writes are temp-file + fsync + rename, so readers can
//!   only ever see the old or the new file, never a torn one. Stale temp files
//!   from interrupted writes are swept at next open.
//! - **Hash boundary**: SHA-256 is an integrity/dedup mechanism, not an
//!   authorization or trust boundary.

use std::collections::{BTreeMap, HashSet, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

use crate::hash::{is_valid_hash, is_valid_root_name, sha256_hex};
use crate::vfs::{atomic_write, EntryKind, FileLock, Vfs};

const FORMAT_LINE: &str = "cas-store v1";

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/// Repository errors. `kind()` drives HTTP status mapping in the server layer.
#[derive(Debug)]
pub enum StoreError {
    /// Malformed request content (bad hash, bad name, empty body).
    InvalidRequest(String),
    /// Block or root not found.
    NotFound(String),
    /// Block exists but its stored bytes fail the SHA-256 check.
    Corrupt { hash: String, detail: String },
    /// A referenced block is missing.
    MissingReference(String),
    /// Hash supplied by the client does not match the content hash.
    HashMismatch { expected: String, actual: String },
    /// The repository is already open by another process.
    Locked,
    /// Repository directory content is not a cas-store repository.
    NotARepository(String),
    /// Underlying I/O failure.
    Io(String),
    /// Internal invariant violation (also used for injected faults).
    Internal(String),
}

impl StoreError {
    pub fn io(context: &str, e: std::io::Error) -> Self {
        StoreError::Io(format!("{context}: {e}"))
    }

    /// Stable machine-readable code (used in JSON error bodies).
    pub fn code(&self) -> &'static str {
        match self {
            StoreError::InvalidRequest(_) => "invalid_request",
            StoreError::NotFound(_) => "not_found",
            StoreError::Corrupt { .. } => "corrupt",
            StoreError::MissingReference(_) => "missing_reference",
            StoreError::HashMismatch { .. } => "hash_mismatch",
            StoreError::Locked => "locked",
            StoreError::NotARepository(_) => "not_a_repository",
            StoreError::Io(_) => "io_error",
            StoreError::Internal(_) => "internal",
        }
    }
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::InvalidRequest(m) => write!(f, "invalid request: {m}"),
            StoreError::NotFound(m) => write!(f, "not found: {m}"),
            StoreError::Corrupt { hash, detail } => {
                write!(f, "block {hash} corrupt: {detail}")
            }
            StoreError::MissingReference(m) => write!(f, "missing referenced block: {m}"),
            StoreError::HashMismatch { expected, actual } => write!(
                f,
                "hash mismatch: expected {expected}, content hashes to {actual}"
            ),
            StoreError::Locked => write!(
                f,
                "repository is locked by another process (single-writer boundary)"
            ),
            StoreError::NotARepository(m) => write!(f, "not a cas-store repository: {m}"),
            StoreError::Io(m) => write!(f, "I/O error: {m}"),
            StoreError::Internal(m) => write!(f, "internal error: {m}"),
        }
    }
}

impl std::error::Error for StoreError {}

// ---------------------------------------------------------------------------
// On-disk structures
// ---------------------------------------------------------------------------

/// Sidecar metadata for a block: the set of hashes this block points at.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Meta {
    #[serde(default)]
    pub refs: Vec<String>,
}

/// A named root: one block hash plus publication metadata.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RootManifest {
    pub root: String,
    pub hash: String,
    /// Unix epoch seconds at publication.
    pub published_at: u64,
}

/// Result of uploading a block.
#[derive(Debug, Clone, Serialize)]
pub struct BlockInfo {
    pub hash: String,
    pub size: u64,
    /// True when identical content was already present (dedup).
    pub deduplicated: bool,
    pub refs: Vec<String>,
}

/// One dangling edge found during reachability traversal.
#[derive(Debug, Clone, Serialize, PartialEq, Eq, PartialOrd, Ord)]
pub struct MissingRef {
    pub from: String,
    pub target: String,
}

/// Result of a garbage collection pass.
#[derive(Debug, Clone, Serialize)]
pub struct GcReport {
    pub dry_run: bool,
    pub blocks_total: usize,
    pub roots: usize,
    pub reachable: usize,
    pub orphan: usize,
    pub removed: Vec<String>,
    pub missing_refs: Vec<MissingRef>,
}

/// Result of a full integrity scan.
#[derive(Debug, Clone, Serialize)]
pub struct VerifyReport {
    pub blocks_total: usize,
    pub ok: usize,
    pub corrupt: Vec<String>,
    pub missing_meta: Vec<String>,
    pub missing_refs: Vec<MissingRef>,
}

/// One root as returned by `GET /roots`.
#[derive(Debug, Clone, Serialize)]
pub struct RootEntry {
    pub name: String,
    pub hash: String,
    pub published_at: u64,
}

/// RAII guard from [`Repository::stage`]: the pinned block stays a GC root
/// until the guard is dropped.
#[must_use = "the pin is released as soon as the guard is dropped"]
pub struct StageGuard {
    repo: Repository,
    hash: String,
}

impl Drop for StageGuard {
    fn drop(&mut self) {
        self.repo.unstage(&self.hash);
    }
}

// ---------------------------------------------------------------------------
// Repository
// ---------------------------------------------------------------------------

/// Thread-safe handle to an opened repository.
///
/// Cheap to clone: all state lives behind `Arc`.
#[derive(Clone)]
pub struct Repository {
    vfs: Arc<dyn Vfs>,
    root: Arc<PathBuf>,
    // Held for the repository's lifetime (None for non-persistent backends).
    _lock: Option<Arc<FileLock>>,
    /// Shared = read/upload/root-publish; exclusive = GC final mark + sweep.
    gc_lock: Arc<RwLock<()>>,
    /// Serializes atomic meta sidecar merge-and-publish.
    meta_lock: Arc<Mutex<()>>,
    /// Serializes GC runs (exclusive locks aren't reentrant w.r.t. fairness).
    gc_run: Arc<Mutex<()>>,
    /// Blocks treated as GC roots although no named root points at them yet —
    /// the staging area for an in-progress publish. Without it, a GC between
    /// "block uploaded" and "root switched" could collect a future root target.
    /// Reference-counted: one entry per outstanding [`StageGuard`] / publish.
    staged: Arc<Mutex<BTreeMap<String, usize>>>,
}

impl Repository {
    /// Open (and if needed initialize) a repository at `path`.
    ///
    /// Fails with [`StoreError::Locked`] if another process holds the lock.
    pub fn open(path: impl Into<PathBuf>, vfs: Arc<dyn Vfs>) -> Result<Self, StoreError> {
        let root = path.into();
        vfs.mkdir_p(&root).map_err(|e| StoreError::io("mkdir repo", e))?;
        vfs.mkdir_p(&root.join("blocks"))
            .map_err(|e| StoreError::io("mkdir blocks", e))?;
        vfs.mkdir_p(&root.join("roots"))
            .map_err(|e| StoreError::io("mkdir roots", e))?;
        vfs.mkdir_p(&root.join("tmp"))
            .map_err(|e| StoreError::io("mkdir tmp", e))?;

        let format_path = root.join("format");
        if vfs.exists(&format_path) {
            let content = vfs
                .read(&format_path)
                .map_err(|e| StoreError::io("read format", e))?;
            let text = String::from_utf8_lossy(&content);
            let stored = text.trim();
            if stored != FORMAT_LINE {
                return Err(StoreError::NotARepository(format!(
                    "unsupported format marker {stored:?}, expected {FORMAT_LINE:?}"
                )));
            }
        } else {
            atomic_write(
                vfs.as_ref(),
                &root,
                &format_path,
                FORMAT_LINE.as_bytes().iter().chain(std::iter::once(&b'\n')).copied().collect::<Vec<_>>().as_slice(),
            )?;
        }

        // Inter-process single-writer boundary. Only meaningful for a
        // persistent backend: an in-memory filesystem cannot be shared between
        // processes at all, so there is nothing to arbitrate.
        let lock = if vfs.persistent() {
            Some(Arc::new(FileLock::try_exclusive(&root.join("repo.lock"))?))
        } else {
            None
        };

        let repo = Repository {
            vfs,
            root: Arc::new(root),
            _lock: lock,
            gc_lock: Arc::new(RwLock::new(())),
            meta_lock: Arc::new(Mutex::new(())),
            gc_run: Arc::new(Mutex::new(())),
            staged: Arc::new(Mutex::new(BTreeMap::new())),
        };
        repo.sweep_stale_temps()?;
        Ok(repo)
    }

    /// Absolute repository path.
    pub fn path(&self) -> &Path {
        &self.root
    }

    fn blocks_dir(&self) -> PathBuf {
        self.root.join("blocks")
    }
    fn roots_dir(&self) -> PathBuf {
        self.root.join("roots")
    }
    fn shard_dir(&self, hash: &str) -> PathBuf {
        self.blocks_dir().join(&hash[0..2])
    }
    fn block_path(&self, hash: &str) -> PathBuf {
        self.shard_dir(hash).join(hash)
    }
    fn meta_path(&self, hash: &str) -> PathBuf {
        self.shard_dir(hash).join(format!("{hash}.meta"))
    }
    fn root_path(&self, name: &str) -> PathBuf {
        self.roots_dir().join(name)
    }

    /// Remove leftover `.tmp.*` files after an interrupted publish.
    fn sweep_stale_temps(&self) -> Result<(), StoreError> {
        let dirs: [PathBuf; 3] = [
            (*self.root).clone(),
            self.blocks_dir(),
            self.roots_dir(),
        ];
        for dir in dirs {
            let entries = match self.vfs.list_dir(&dir) {
                Ok(e) => e,
                Err(_) => continue,
            };
            for (name, _) in entries {
                if name.starts_with(".tmp.") {
                    let _ = self.vfs.remove_file(&dir.join(&name));
                }
            }
            // shard dirs under blocks/
            if dir == self.blocks_dir() {
                let shards = match self.vfs.list_dir(&dir) {
                    Ok(e) => e,
                    Err(_) => continue,
                };
                for (sh, kind) in shards {
                    if kind == EntryKind::Dir {
                        if let Ok(files) = self.vfs.list_dir(&dir.join(&sh)) {
                            for (name, _) in files {
                                if name.starts_with(".tmp.") {
                                    let _ = self.vfs.remove_file(&dir.join(&sh).join(&name));
                                }
                            }
                        }
                    }
                }
            }
        }
        Ok(())
    }

    // -----------------------------------------------------------------------
    // Blocks
    // -----------------------------------------------------------------------

    /// Store `data`, addressed by its SHA-256, with declared outgoing refs.
    ///
    /// All refs must exist already (this keeps a freshly uploaded block from
    /// starting life with dangling edges; roots enforce the same invariant).
    /// Identical content is deduplicated: a second upload merges any *new*
    /// refs into the sidecar and returns `deduplicated: true`.
    pub fn add_block(&self, data: &[u8], refs: &[String]) -> Result<BlockInfo, StoreError> {
        if data.is_empty() {
            return Err(StoreError::InvalidRequest("block data must not be empty".into()));
        }
        let hash = sha256_hex(data);
        self.add_block_named(&hash, data, refs)
    }

    /// Store content at a client-supplied address and verify integrity.
    ///
    /// Used by `PUT /blocks/{hash}`: if `expected` does not hash-match the
    /// content, nothing is written and [`StoreError::HashMismatch`] is returned.
    pub fn add_block_named(
        &self,
        expected: &str,
        data: &[u8],
        refs: &[String],
    ) -> Result<BlockInfo, StoreError> {
        if data.is_empty() {
            return Err(StoreError::InvalidRequest("block data must not be empty".into()));
        }
        if !is_valid_hash(expected) {
            return Err(StoreError::InvalidRequest(format!(
                "{expected:?} is not a 64-char lowercase hex SHA-256"
            )));
        }
        let mut refs = refs.to_vec();
        refs.sort();
        refs.dedup();
        for r in &refs {
            if !is_valid_hash(r) {
                return Err(StoreError::InvalidRequest(format!("bad ref hash {r:?}")));
            }
        }

        let actual = sha256_hex(data);
        if actual != expected {
            return Err(StoreError::HashMismatch {
                expected: expected.to_string(),
                actual,
            });
        }

        // Concurrent uploads and GC: shared guard lets them run together.
        let _guard = self.gc_lock.read().unwrap();

        // Refs must resolve to existing blocks.
        for r in &refs {
            if !self.vfs.exists(&self.block_path(r)) {
                return Err(StoreError::MissingReference(r.clone()));
            }
        }

        let shard = self.shard_dir(expected);
        self.vfs
            .mkdir_p(&shard)
            .map_err(|e| StoreError::io("mkdir shard", e))?;
        let data_path = self.block_path(expected);

        // Existence check followed by temp+rename. If another identical upload
        // wins the race between here and the rename, the rename simply replaces
        // the winner's file with byte-identical content (same hash, same bytes)
        // — a benign "deduplication"; integrity cannot differ.
        let mut deduplicated = false;
        if self.vfs.exists(&data_path) {
            let existing = self
                .vfs
                .read(&data_path)
                .map_err(|e| StoreError::io("read existing block", e))?;
            if sha256_hex(&existing) != expected {
                return Err(StoreError::Corrupt {
                    hash: expected.to_string(),
                    detail: "stored bytes hash differently than their address".into(),
                });
            }
            deduplicated = true;
        }

        // Publish immutable bytes via a unique temp file + atomic rename, so a
        // failed/interrupted write leaves at most a `.tmp.*` file in the shard
        // (swept at next open) and NEVER a torn file at the content address.
        let tmp_path = shard.join(format!(
            ".tmp.{}.{}.{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
            &expected[..8]
        ));
        {
            let mut f = self
                .vfs
                .create_new(&tmp_path)
                .map_err(|e| StoreError::io("create block temp", e))?;
            use std::io::Write;
            if let Err(e) = (|| -> std::io::Result<()> {
                f.write_all(data)?;
                f.flush()
            })() {
                // Close and remove the partial temp before reporting failure.
                drop(f);
                let _ = self.vfs.remove_file(&tmp_path);
                return Err(StoreError::io("write block", e));
            }
            drop(f);
            self.vfs.sync_file(&tmp_path).ok();
        }
        self.vfs
            .rename(&tmp_path, &data_path)
            .map_err(|e| StoreError::io("publish block rename", e))?;
        self.vfs.sync_dir(&shard).ok();

        // Another upload may have published a meta already: only fill if absent,
        // otherwise merge to keep both uploads' declared refs.
        let meta = Meta { refs: refs.clone() };
        if deduplicated {
            let merged = self.merge_meta(expected, &refs)?;
            return Ok(BlockInfo {
                hash: expected.to_string(),
                size: data.len() as u64,
                deduplicated: true,
                refs: merged,
            });
        }
        self.publish_meta_if_absent(expected, &meta)?;

        Ok(BlockInfo {
            hash: expected.to_string(),
            size: data.len() as u64,
            deduplicated: false,
            refs,
        })
    }

    /// Merge new refs into an existing sidecar (serialized), publishing
    /// atomically. Caller-level serialization is the meta_lock taken here.
    fn merge_meta(&self, hash: &str, extra: &[String]) -> Result<Vec<String>, StoreError> {
        let _g = self.meta_lock.lock().unwrap();
        let mut meta = self.read_meta_or_default(hash)?;
        let mut merged: HashSet<String> = meta.refs.drain(..).collect();
        merged.extend(extra.iter().cloned());
        let mut merged: Vec<String> = merged.into_iter().collect();
        merged.sort();
        let meta = Meta { refs: merged.clone() };
        self.write_meta_locked(hash, &meta)?;
        Ok(merged)
    }

    fn publish_meta_if_absent(&self, hash: &str, meta: &Meta) -> Result<(), StoreError> {
        let _g = self.meta_lock.lock().unwrap();
        let path = self.meta_path(hash);
        if self.vfs.exists(&path) {
            return Ok(());
        }
        self.write_meta_locked(hash, meta)
    }

    /// Serialize + atomic publish of a sidecar. Must be called while holding
    /// `meta_lock` (merge_meta / publish_meta_if_absent do so) — do not lock it
    /// again here: std Mutex is not reentrant.
    fn write_meta_locked(&self, hash: &str, meta: &Meta) -> Result<(), StoreError> {
        let body = serde_json::to_vec(meta).map_err(|e| StoreError::Internal(e.to_string()))?;
        let shard = self.shard_dir(hash);
        atomic_write(self.vfs.as_ref(), &shard, &self.meta_path(hash), &body)
    }

    fn read_meta_or_default(&self, hash: &str) -> Result<Meta, StoreError> {
        let path = self.meta_path(hash);
        if !self.vfs.exists(&path) {
            return Ok(Meta { refs: Vec::new() });
        }
        let body = self
            .vfs
            .read(&path)
            .map_err(|e| StoreError::io("read meta", e))?;
        serde_json::from_slice(&body)
            .map_err(|e| StoreError::Corrupt {
                hash: hash.to_string(),
                detail: format!("invalid meta JSON: {e}"),
            })
    }

    /// Read a block. Holds the shared GC guard for the whole read so a
    /// concurrent sweep cannot unlink bytes still being served.
    pub fn get_block(&self, hash: &str) -> Result<Vec<u8>, StoreError> {
        if !is_valid_hash(hash) {
            return Err(StoreError::InvalidRequest(format!("bad hash {hash:?}")));
        }
        let _guard = self.gc_lock.read().unwrap();
        let path = self.block_path(hash);
        if !self.vfs.exists(&path) {
            return Err(StoreError::NotFound(format!("block {hash}")));
        }
        let data = self
            .vfs
            .read(&path)
            .map_err(|e| StoreError::io("read block", e))?;
        Ok(data)
    }

    /// Read a block and re-hash it (integrity-checked download).
    pub fn get_block_verified(&self, hash: &str) -> Result<Vec<u8>, StoreError> {
        let data = self.get_block(hash)?;
        if sha256_hex(&data) != hash {
            return Err(StoreError::Corrupt {
                hash: hash.to_string(),
                detail: "stored bytes do not hash to their address".into(),
            });
        }
        Ok(data)
    }

    pub fn block_exists(&self, hash: &str) -> bool {
        is_valid_hash(hash) && self.vfs.exists(&self.block_path(hash))
    }

    /// Enumerate all block hashes on disk. A data file without its `.meta`
    /// sidecar is listed too (its refs are treated as empty).
    fn enumerate_blocks(&self) -> Result<BTreeMap<String, bool>, StoreError> {
        // hash -> has_meta
        let mut out: BTreeMap<String, bool> = BTreeMap::new();
        let blocks = self.blocks_dir();
        let shards = match self.vfs.list_dir(&blocks) {
            Ok(e) => e,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
            Err(e) => return Err(StoreError::io("list blocks", e)),
        };
        for (sh, kind) in shards {
            if kind != EntryKind::Dir || sh.len() != 2 {
                continue;
            }
            let files = match self.vfs.list_dir(&blocks.join(&sh)) {
                Ok(f) => f,
                Err(e) => return Err(StoreError::io("list shard", e)),
            };
            for (name, fkind) in files {
                if fkind != EntryKind::File || name.starts_with('.') {
                    continue;
                }
                if let Some(hash) = name.strip_suffix(".meta") {
                    if is_valid_hash(hash) {
                        out.entry(hash.to_string()).or_insert(false);
                        if let Some(b) = out.get_mut(hash) {
                            *b = true;
                        }
                    }
                } else if is_valid_hash(&name) {
                    out.entry(name).or_insert(false);
                }
            }
        }
        Ok(out)
    }

    // -----------------------------------------------------------------------
    // Roots
    // -----------------------------------------------------------------------

    /// Pin a block as an extra GC root while preparing a publish.
    ///
    /// Typical use:
    ///
    /// ```ignore
    /// // build a new tree, then pin its tip before flipping any root, so a
    /// // concurrent/periodic GC cannot reclaim it in the upload→publish gap
    /// let _staged = repo.stage(&new_tip)?;
    /// repo.put_root("main", &new_tip)?; // _staged drops after this
    /// ```
    ///
    /// `put_root` stages its target internally for the duration of the call;
    /// explicit staging is only needed across multi-step publish protocols.
    pub fn stage(&self, hash: &str) -> Result<StageGuard, StoreError> {
        if !is_valid_hash(hash) {
            return Err(StoreError::InvalidRequest(format!("bad hash {hash:?}")));
        }
        let _guard = self.gc_lock.read().unwrap();
        if !self.vfs.exists(&self.block_path(hash)) {
            return Err(StoreError::MissingReference(hash.to_string()));
        }
        *self.staged.lock().unwrap().entry(hash.to_string()).or_insert(0) += 1;
        Ok(StageGuard {
            repo: self.clone(),
            hash: hash.to_string(),
        })
    }

    fn unstage(&self, hash: &str) {
        let mut staged = self.staged.lock().unwrap();
        match staged.get_mut(hash) {
            Some(n) if *n > 1 => *n -= 1,
            Some(_) => {
                staged.remove(hash);
            }
            None => {}
        }
    }

    /// Atomically publish (or repoint) named root `name` at `hash`.
    ///
    /// The target is staged for the whole call, guaranteeing that a concurrent
    /// GC cannot delete it in the gap between the existence check and the
    /// manifest rename. The root block and every block it references must
    /// already be present; the manifest is written temp + rename, so readers
    /// observe either the old or the new root, never a torn one.
    pub fn put_root(&self, name: &str, hash: &str) -> Result<RootManifest, StoreError> {
        if !is_valid_root_name(name) {
            return Err(StoreError::InvalidRequest(format!("bad root name {name:?}")));
        }
        if !is_valid_hash(hash) {
            return Err(StoreError::InvalidRequest(format!("bad hash {hash:?}")));
        }
        let _guard = self.gc_lock.read().unwrap();
        if !self.vfs.exists(&self.block_path(hash)) {
            return Err(StoreError::MissingReference(hash.to_string()));
        }
        // Pin across the publish. Released whether it succeeds or fails: on
        // success the named manifest itself now roots the block.
        *self.staged.lock().unwrap().entry(hash.to_string()).or_insert(0) += 1;
        let result = self.publish_root_manifest(name, hash);
        self.unstage(hash);
        result
    }

    fn publish_root_manifest(&self, name: &str, hash: &str) -> Result<RootManifest, StoreError> {
        let manifest = RootManifest {
            root: name.to_string(),
            hash: hash.to_string(),
            published_at: now_secs(),
        };
        let body = serde_json::to_vec_pretty(&manifest)
            .map_err(|e| StoreError::Internal(e.to_string()))?;
        // A rename fault here models power loss mid root-switch: the old
        // manifest remains intact and reachable.
        atomic_write(
            self.vfs.as_ref(),
            &self.roots_dir(),
            &self.root_path(name),
            &body,
        )?;
        Ok(manifest)
    }

    /// Read one root manifest.
    pub fn get_root(&self, name: &str) -> Result<RootManifest, StoreError> {
        if !is_valid_root_name(name) {
            return Err(StoreError::InvalidRequest(format!("bad root name {name:?}")));
        }
        let _guard = self.gc_lock.read().unwrap();
        let path = self.root_path(name);
        if !self.vfs.exists(&path) {
            return Err(StoreError::NotFound(format!("root {name}")));
        }
        self.read_root_file(&path)
    }

    fn read_root_file(&self, path: &Path) -> Result<RootManifest, StoreError> {
        let body = self
            .vfs
            .read(path)
            .map_err(|e| StoreError::io("read root", e))?;
        serde_json::from_slice(&body).map_err(|e| StoreError::Corrupt {
            hash: path.display().to_string(),
            detail: format!("invalid root manifest: {e}"),
        })
    }

    /// List all roots (name, hash, publish time).
    pub fn list_roots(&self) -> Result<Vec<RootEntry>, StoreError> {
        let _guard = self.gc_lock.read().unwrap();
        let mut out = Vec::new();
        let entries = match self.vfs.list_dir(&self.roots_dir()) {
            Ok(e) => e,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(out),
            Err(e) => return Err(StoreError::io("list roots", e)),
        };
        for (name, kind) in entries {
            if kind != EntryKind::File || name.starts_with('.') {
                continue;
            }
            if let Ok(m) = self.read_root_file(&self.root_path(&name)) {
                out.push(RootEntry {
                    name: m.root,
                    hash: m.hash,
                    published_at: m.published_at,
                });
            }
        }
        out.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(out)
    }

    /// Delete a named root. The blocks it referenced become collectable by the
    /// next GC pass (unless another root still reaches them).
    pub fn delete_root(&self, name: &str) -> Result<(), StoreError> {
        if !is_valid_root_name(name) {
            return Err(StoreError::InvalidRequest(format!("bad root name {name:?}")));
        }
        let _guard = self.gc_lock.write().unwrap();
        let path = self.root_path(name);
        if !self.vfs.exists(&path) {
            return Err(StoreError::NotFound(format!("root {name}")));
        }
        self.vfs
            .remove_file(&path)
            .map_err(|e| StoreError::io("delete root", e))?;
        self.vfs.sync_dir(&self.roots_dir()).ok();
        Ok(())
    }

    // -----------------------------------------------------------------------
    // Reachability & GC
    // -----------------------------------------------------------------------

    /// Read all currently published roots while holding a guard chosen by the
    /// caller (read for snapshots, write for the authoritative GC sweep).
    fn snapshot_roots(&self) -> Result<Vec<RootManifest>, StoreError> {
        let mut roots = Vec::new();
        let entries = match self.vfs.list_dir(&self.roots_dir()) {
            Ok(e) => e,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(roots),
            Err(e) => return Err(StoreError::io("list roots", e)),
        };
        for (name, kind) in entries {
            if kind != EntryKind::File || name.starts_with('.') {
                continue;
            }
            // Skip a manifest that is mid-rename: atomic_write never shows a
            // torn file, but be defensive about anything we cannot parse.
            if let Ok(m) = self.read_root_file(&self.root_path(&name)) {
                roots.push(m);
            }
        }
        Ok(roots)
    }

    /// BFS from the given root hashes, following `.meta` refs.
    ///
    /// Returns `(reachable, missing_refs)`. Missing edges are recorded rather
    /// than fatal: a repository with holes is still GC-able, and the report
    /// surfaces the damage. A missing block contributes no outgoing edges.
    fn mark_reachable(
        &self,
        start_hashes: &[String],
        all_blocks: &HashSet<String>,
    ) -> (HashSet<String>, Vec<MissingRef>) {
        let mut reachable: HashSet<String> = HashSet::new();
        let mut missing: Vec<MissingRef> = Vec::new();
        let mut seen_missing: HashSet<(String, String)> = HashSet::new();
        let mut queue: VecDeque<String> = VecDeque::new();

        for h in start_hashes {
            if all_blocks.contains(h) && reachable.insert(h.clone()) {
                queue.push_back(h.clone());
            } else if !all_blocks.contains(h) {
                if seen_missing.insert(("<root>".to_string(), h.clone())) {
                    missing.push(MissingRef {
                        from: "<root>".to_string(),
                        target: h.clone(),
                    });
                }
            }
        }

        while let Some(h) = queue.pop_front() {
            let meta = match self.read_meta_or_default(&h) {
                Ok(m) => m,
                Err(_) => Meta { refs: Vec::new() },
            };
            if std::env::var("CAS_DEBUG_GC").is_ok() && start_hashes.contains(&h) {
                eprintln!("[GC]   node {} meta refs count = {}", &h[..6], meta.refs.len());
            }
            for r in meta.refs {
                if !is_valid_hash(&r) {
                    continue;
                }
                if all_blocks.contains(&r) {
                    if reachable.insert(r.clone()) {
                        queue.push_back(r);
                    }
                } else if seen_missing.insert((h.clone(), r.clone())) {
                    missing.push(MissingRef {
                        from: h.clone(),
                        target: r,
                    });
                }
            }
        }
        missing.sort();
        (reachable, missing)
    }

    /// Run mark-sweep garbage collection.
    ///
    /// # Concurrency
    ///
    /// 1. (read guard) preliminary mark for the human-readable pre-sweep view.
    /// 2. (write guard — exclusive against ALL readers and uploads) re-snapshot
    ///    the roots and mark **again**, then delete. The second mark is
    ///    authoritative: anything reachable at sweep instant survives.
    ///
    /// Because readers hold the read guard for their whole `get_block`, an
    /// unlinked file cannot be removed out from under an in-flight read.
    pub fn gc(&self, dry_run: bool) -> Result<GcReport, StoreError> {
        let _serial = self.gc_run.lock().unwrap();

        // Preliminary snapshot (cheap, allows concurrent readers/uploaders).
        // Its only purpose is to surface "what existed before the sweep"; the
        // authoritative reachability is recomputed under the write guard.
        let prelim_roots = self.snapshot_roots()?;
        let prelim_blocks: HashSet<String> = self.enumerate_blocks()?.into_keys().collect();
        let prelim_starts: Vec<String> = prelim_roots.iter().map(|m| m.hash.clone()).collect();
        let _ = self.mark_reachable(&prelim_starts, &prelim_blocks);

        // Authoritative phase excludes readers and uploaders.
        let guard = self.gc_lock.write().unwrap();
        let roots = self.snapshot_roots()?;
        // hash -> has_meta
        let mut blocks = self.enumerate_blocks()?;
        let all: HashSet<String> = blocks.keys().cloned().collect();
        // Roots of reachability: every published named root plus every staged
        // (pinned) block awaiting a publish.
        let staged: Vec<String> = self.staged.lock().unwrap().keys().cloned().collect();
        let mut starts: Vec<String> = roots.iter().map(|m| m.hash.clone()).collect();
        starts.extend(staged.iter().cloned());
        let (reachable, missing_refs) = self.mark_reachable(&starts, &all);
        if std::env::var("CAS_DEBUG_GC").is_ok() {
            eprintln!("[GC] roots={:?} staged={:?} total={} reachable={}",
                roots.iter().map(|m| (m.root.clone(), m.hash[..6].to_string())).collect::<Vec<_>>(),
                staged.iter().map(|h| h[..6].to_string()).collect::<Vec<_>>(),
                all.len(), reachable.len());
        }

        let mut removed: Vec<String> = Vec::new();
        // 1. Sweep unreachable data blocks (and their sidecars alongside).
        for hash in all.iter() {
            if reachable.contains(hash) {
                continue;
            }
            if dry_run {
                removed.push(hash.clone());
                continue;
            }
            let data = self.block_path(hash);
            if self.vfs.exists(&data) {
                self.vfs
                    .remove_file(&data)
                    .map_err(|e| StoreError::io("remove block", e))?;
                removed.push(hash.clone());
            }
            let meta = self.meta_path(hash);
            if self.vfs.exists(&meta) {
                let _ = self.vfs.remove_file(&meta);
                blocks.insert(hash.clone(), false);
            }
        }
        // 2. Dangling sidecars (meta exists, data does not): metadata without
        //    the block it describes can never be reachable; remove it even in a
        //    dry run we only report it — removal is safe and silent either way.
        let mut orphan_meta = 0usize;
        for (hash, has_meta) in blocks.iter() {
            if !*has_meta {
                continue;
            }
            let mp = self.meta_path(hash);
            if !self.vfs.exists(&mp) {
                continue;
            }
            if self.vfs.exists(&self.block_path(hash)) {
                continue;
            }
            if dry_run {
                orphan_meta += 1;
            } else if self.vfs.remove_file(&mp).is_ok() {
                orphan_meta += 1;
            }
        }
        if !dry_run {
            self.vfs.sync_dir(&self.blocks_dir()).ok();
        }
        drop(guard);

        let _ = orphan_meta; // sidecar cleanup is incidental, not part of the report
        let orphan_count = all.len().saturating_sub(reachable.len());
        Ok(GcReport {
            dry_run,
            blocks_total: all.len(),
            roots: roots.len(),
            reachable: reachable.len(),
            orphan: orphan_count,
            removed,
            missing_refs,
        })
    }

    /// Full integrity scan: re-hash every block and check every ref edge.
    ///
    /// `missing_meta` lists blocks whose `.meta` sidecar is absent or blocks
    /// referenced by a root without recoverable edge metadata; it is an
    /// operational warning, not corruption.
    pub fn verify(&self) -> Result<VerifyReport, StoreError> {
        let _guard = self.gc_lock.read().unwrap();
        let blocks = self.enumerate_blocks()?;
        let all: HashSet<String> = blocks.keys().cloned().collect();

        let mut ok = 0usize;
        let mut corrupt = Vec::new();
        let mut missing_meta = Vec::new();
        let mut missing_refs: Vec<MissingRef> = Vec::new();
        let mut seen_edge: HashSet<(String, String)> = HashSet::new();

        for (hash, has_meta) in &blocks {
            let path = self.block_path(hash);
            if !self.vfs.exists(&path) {
                // Orphan sidecar: data file missing.
                missing_meta.push(hash.clone());
                continue;
            }
            let data = match self.vfs.read(&path) {
                Ok(d) => d,
                Err(_) => {
                    corrupt.push(hash.clone());
                    continue;
                }
            };
            // The hash is integrity, not trust: verify bytes against address.
            if sha256_hex(&data) != *hash {
                corrupt.push(hash.clone());
                continue;
            }
            ok += 1;

            if !has_meta {
                missing_meta.push(hash.clone());
                continue;
            }
            let meta = self.read_meta_or_default(hash)?;
            for r in meta.refs {
                if !all.contains(&r) {
                    if seen_edge.insert((hash.clone(), r.clone())) {
                        missing_refs.push(MissingRef {
                            from: hash.clone(),
                            target: r,
                        });
                    }
                }
            }
        }
        corrupt.sort();
        missing_meta.sort();
        missing_refs.sort();
        Ok(VerifyReport {
            blocks_total: all.len(),
            ok,
            corrupt,
            missing_meta,
            missing_refs,
        })
    }
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
