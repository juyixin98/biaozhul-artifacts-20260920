//! Content-addressed object store with manifest reference counting and
//! concurrency-safe garbage collection.
//!
//! # Object model
//!
//! There are two kinds of objects, both keyed by the hex SHA-256 of their
//! bytes (so storage is *content-addressed*):
//!
//! * **Data blocks**  - opaque bytes, reference nothing.
//! * **Manifests**    - JSON of the form `{"refs": ["<hash>", ...]}`, naming
//!   other objects (manifests or blocks) they reference.
//!
//! **Published roots** are named pointers to an object. The live set is every
//! object reachable from any root by following manifest references. Objects
//! not reachable from any root are garbage and may be collected by
//! [`Store::gc`].
//!
//! # Safety against a concurrent publish
//!
//! All root mutation (publish/delete) and every garbage collection run take
//! the *same* exclusive `gc_lock`. A publish therefore either completes before
//! GC snapshots the root set or blocks until that GC run finishes; GC can never
//! observe the "root deleted but replacement not yet published" intermediate
//! state and so can never reclaim an object that a newly published root makes
//! reachable.
//!
//! # Unfinished uploads
//!
//! Objects may be parked via [`Store::begin_upload`] / [`Store::complete_upload`].
//! While an upload is open its objects live in a separate retention set with
//! an independent retention period; they survive GC even though no root points
//! at them. Only uploads that were *completed or aborted more than*
//! `upload_retention` ago fall out of the retention set.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Mutex, RwLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use sha2::{Digest, Sha256};

/// Errors returned by store operations.
#[derive(Debug)]
pub enum Error {
    Io(std::io::Error),
    Json(serde_json::Error),
    /// Object id is not a 64-char lowercase hex SHA-256.
    InvalidHash(String),
    /// Referenced object does not exist (used when validating a manifest).
    MissingDependency(String),
    /// Manifest JSON is not an object with a string array `refs`.
    InvalidManifest(String),
    /// No such upload session.
    UploadNotFound(String),
    /// Upload session is still open and may not be completed/aborted twice.
    UploadStillOpen(String),
    /// Attempt to publish a root pointing at a missing object.
    ObjectNotFound(String),
}

impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Error::Io(e) => write!(f, "io error: {e}"),
            Error::Json(e) => write!(f, "json error: {e}"),
            Error::InvalidHash(h) => write!(f, "invalid object hash: {h}"),
            Error::MissingDependency(h) => write!(f, "referenced object missing: {h}"),
            Error::InvalidManifest(m) => write!(f, "invalid manifest: {m}"),
            Error::UploadNotFound(id) => write!(f, "upload not found: {id}"),
            Error::UploadStillOpen(id) => write!(f, "upload already finalized: {id}"),
            Error::ObjectNotFound(h) => write!(f, "object not found: {h}"),
        }
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Error::Io(e)
    }
}

impl From<serde_json::Error> for Error {
    fn from(e: serde_json::Error) -> Self {
        Error::Json(e)
    }
}

pub type Result<T> = std::result::Result<T, Error>;

/// Verify `hash` is 64 lowercase hex chars and return it unchanged.
pub fn validate_hash(hash: &str) -> Result<&str> {
    if hash.len() == 64
        && hash
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        Ok(hash)
    } else {
        Err(Error::InvalidHash(hash.to_string()))
    }
}

/// Compute the content hash (hex SHA-256) of `data`.
pub fn hash_bytes(data: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(data);
    hex::encode(h.finalize())
}

/// Parse a manifest body and return the deduplicated, order-preserving list of
/// references it declares.
pub fn parse_manifest(body: &[u8]) -> Result<Vec<String>> {
    let v: serde_json::Value = serde_json::from_slice(body)?;
    let arr = v
        .get("refs")
        .and_then(|r| r.as_array())
        .ok_or_else(|| Error::InvalidManifest("expected object with a `refs` array".into()))?;
    let mut out = Vec::new();
    let mut seen = BTreeSet::new();
    for item in arr {
        let r = item
            .as_str()
            .ok_or_else(|| Error::InvalidManifest("every ref must be a string".into()))?;
        validate_hash(r)?;
        if seen.insert(r.to_string()) {
            out.push(r.to_string());
        }
    }
    Ok(out)
}

/// State of an upload session.
struct UploadRec {
    /// Objects parked by this session.
    objects: BTreeSet<String>,
    /// `None` while open; otherwise completion/abort timestamp (secs since epoch).
    finalized_at: Option<u64>,
}

/// Whether an object is an opaque block or a reference-bearing manifest.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ObjectKind {
    Block,
    Manifest,
}

/// Result of a garbage collection pass.
#[derive(Debug, Clone, Serialize, PartialEq, Eq)]
pub struct GcReport {
    /// Objects kept because a root can reach them.
    pub reachable: usize,
    /// Objects kept solely by an unfinished / recently-finished upload.
    pub retained_by_upload: usize,
    /// Objects deleted by this pass.
    pub deleted: Vec<String>,
    /// Open upload sessions whose objects were retained.
    pub open_uploads: usize,
}

/// The content-addressed object store. Cheaply cloneable; all state is shared.
#[derive(Clone)]
pub struct Store {
    root: PathBuf,
    /// Set of objects currently registered on disk. Held as a RwLock so that
    /// single puts/gets are concurrent; GC takes a write lock to freeze it.
    objects: std::sync::Arc<RwLock<BTreeSet<String>>>,
    /// Explicit kind of each object, so a data block whose bytes happen to
    /// look like manifest JSON is never traversed as one.
    kinds: std::sync::Arc<RwLock<BTreeMap<String, ObjectKind>>>,
    /// Upload sessions (open and recently finalized), keyed by upload id.
    uploads: std::sync::Arc<Mutex<HashMap<String, UploadRec>>>,
    /// Named published roots: name -> object hash.
    roots: std::sync::Arc<Mutex<BTreeMap<String, String>>>,
    /// Exclusive lock serializing root mutation against collection.
    gc_lock: std::sync::Arc<tokio::sync::RwLock<()>>,
    upload_retention_secs: u64,
}

impl Store {
    /// Open (or create) a store rooted at `dir`.
    ///
    /// `upload_retention_secs` is the independent grace period during which a
    /// *finalized* upload's objects remain protected from GC. Open uploads are
    /// always retained regardless of age.
    pub fn open(dir: impl AsRef<Path>, upload_retention_secs: u64) -> Result<Self> {
        let root = dir.as_ref().to_path_buf();
        fs::create_dir_all(root.join("objects"))?;
        fs::create_dir_all(root.join("tmp"))?;

        let mut objects = BTreeSet::new();
        for entry in fs::read_dir(root.join("objects"))? {
            let entry = entry?;
            if !entry.file_type()?.is_file() {
                continue;
            }
            let name = entry.file_name().to_string_lossy().to_string();
            if validate_hash(&name).is_ok() {
                objects.insert(name);
            }
        }

        let mut roots = BTreeMap::new();
        let roots_path = root.join("roots.json");
        if roots_path.exists() {
            let raw = fs::read(&roots_path)?;
            if !raw.is_empty() {
                roots = serde_json::from_slice(&raw)?;
            }
        }

        // Kind metadata is an index over the objects; a missing file means a
        // legacy/fresh directory where every object defaults to a block.
        let mut kinds: BTreeMap<String, ObjectKind> = BTreeMap::new();
        let kinds_path = root.join("kinds.json");
        if kinds_path.exists() {
            let raw = fs::read(&kinds_path)?;
            if !raw.is_empty() {
                kinds = serde_json::from_slice(&raw)?;
            }
        } else {
            for h in &objects {
                kinds.insert(h.clone(), ObjectKind::Block);
            }
        }

        Ok(Store {
            root,
            objects: std::sync::Arc::new(RwLock::new(objects)),
            kinds: std::sync::Arc::new(RwLock::new(kinds)),
            uploads: std::sync::Arc::new(Mutex::new(HashMap::new())),
            roots: std::sync::Arc::new(Mutex::new(roots)),
            gc_lock: std::sync::Arc::new(tokio::sync::RwLock::new(())),
            upload_retention_secs,
        })
    }

    pub fn retention_secs(&self) -> u64 {
        self.upload_retention_secs
    }

    fn now_secs() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
    }

    fn object_path(&self, hash: &str) -> PathBuf {
        self.root.join("objects").join(hash)
    }

    fn tmp_path(&self, token: &str) -> PathBuf {
        self.root.join("tmp").join(token)
    }

    /// Atomically place `data` in the store as a data block and return its
    /// content hash.
    ///
    /// Writing goes through a temp file + rename so a partially written object
    /// is never visible. Idempotent: putting the same content twice is a no-op.
    pub fn put_block(&self, data: &[u8]) -> Result<String> {
        let hash = hash_bytes(data);
        self.put_at(&hash, data, ObjectKind::Block)?;
        Ok(hash)
    }

    /// Atomically place a manifest body. The body is validated and the hash is
    /// derived from its bytes, exactly like a block.
    pub fn put_manifest(&self, refs: &[String]) -> Result<String> {
        // Canonical, deterministic encoding: a `refs` array, deduped in order.
        let mut seen = BTreeSet::new();
        let mut ordered = Vec::new();
        for r in refs {
            validate_hash(r)?;
            if seen.insert(r.clone()) {
                ordered.push(r.clone());
            }
        }
        let body = serde_json::to_vec(&serde_json::json!({ "refs": ordered }))?;
        let hash = hash_bytes(&body);
        self.put_at(&hash, &body, ObjectKind::Manifest)?;
        Ok(hash)
    }

    /// Internal: write bytes for a known hash via temp-file + atomic rename.
    ///
    /// The whole "write temp -> rename into objects/ -> publish in the index
    /// and kind map" sequence runs under the object-index write lock, which a
    /// GC pass also holds for its entire mark-sweep. A put and a collection are
    /// therefore mutually exclusive: either the object (file + index entry)
    /// becomes visible before GC snapshots the index, or GC finishes first and
    /// the object is added afterwards. There is no window where the file exists
    /// without an index entry (in which GC would unlink it out from under the
    /// writer).
    fn put_at(&self, hash: &str, data: &[u8], kind: ObjectKind) -> Result<()> {
        validate_hash(hash)?;
        // Fast path under a read lock.
        if self.objects.read().unwrap().contains(hash) {
            return Ok(());
        }
        // Hold the write lock across BOTH the rename and the index insertion so
        // GC cannot sweep the file in between.
        let mut objs = self.objects.write().unwrap();
        if objs.contains(hash) {
            return Ok(()); // another writer won the race
        }
        let token = format!(
            "{}.{}.tmp",
            hash,
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let tmp = self.tmp_path(&token);
        fs::write(&tmp, data)?;
        let dst = self.object_path(hash);
        fs::rename(&tmp, &dst)?;
        objs.insert(hash.to_string());
        // Publish the kind while still holding the index lock so GC never sees
        // an indexed manifest whose kind has not been recorded yet.
        self.kinds.write().unwrap().insert(hash.to_string(), kind);
        drop(objs);
        self.persist_kinds()?;
        Ok(())
    }

    /// Store a block as part of an upload session.
    ///
    /// The on-disk write, the index publication and the upload-retention
    /// registration happen as one atomic step under the object-index write
    /// lock (which GC also holds for its whole pass). A collector therefore can
    /// never observe the object "on disk but neither root-reachable nor in an
    /// open upload" and reclaim it mid-upload.
    pub fn put_for_upload(&self, upload_id: &str, data: &[u8]) -> Result<String> {
        let hash = hash_bytes(data);
        self.put_at_in_upload(&hash, data, ObjectKind::Block, upload_id)?;
        Ok(hash)
    }

    /// Store a manifest as part of an upload session (atomic retention as
    /// above).
    pub fn put_manifest_for_upload(
        &self,
        upload_id: &str,
        refs: &[String],
    ) -> Result<String> {
        let mut seen = BTreeSet::new();
        let mut ordered = Vec::new();
        for r in refs {
            validate_hash(r)?;
            if seen.insert(r.clone()) {
                ordered.push(r.clone());
            }
        }
        let body = serde_json::to_vec(&serde_json::json!({ "refs": ordered }))?;
        let hash = hash_bytes(&body);
        self.put_at_in_upload(&hash, &body, ObjectKind::Manifest, upload_id)?;
        Ok(hash)
    }

    /// Shared body for atomic "write + publish + retain under an upload".
    fn put_at_in_upload(
        &self,
        hash: &str,
        data: &[u8],
        kind: ObjectKind,
        upload_id: &str,
    ) -> Result<()> {
        validate_hash(hash)?;
        if self.objects.read().unwrap().contains(hash) {
            // Already present: just make sure the upload retains it too.
            self.register_upload_object(upload_id, hash)?;
            return Ok(());
        }

        // Hold the index write lock for the write, the index insert AND the
        // upload-retention registration, so GC (which holds this same lock for
        // its whole pass) cannot run anywhere in between.
        let mut objs = self.objects.write().unwrap();
        if !objs.contains(hash) {
            let token = format!(
                "{}.{}.tmp",
                hash,
                SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap()
                    .as_nanos()
            );
            let tmp = self.tmp_path(&token);
            fs::write(&tmp, data)?;
            fs::rename(&tmp, self.object_path(hash))?;
            objs.insert(hash.to_string());
            self.kinds.write().unwrap().insert(hash.to_string(), kind);
        }
        // Register retention while STILL holding the index lock. Lock order is
        // index -> uploads, identical to GC, so this cannot deadlock, and there
        // is no instant where the object is published but unretained.
        self.register_upload_object(upload_id, hash)?;
        drop(objs);

        self.persist_kinds()?;
        Ok(())
    }

    fn register_upload_object(&self, upload_id: &str, hash: &str) -> Result<()> {
        let mut ups = self.uploads.lock().unwrap();
        let rec = ups
            .get_mut(upload_id)
            .ok_or_else(|| Error::UploadNotFound(upload_id.to_string()))?;
        if rec.finalized_at.is_some() {
            return Err(Error::UploadStillOpen(upload_id.to_string()));
        }
        rec.objects.insert(hash.to_string());
        Ok(())
    }

    /// Read an object's bytes.
    pub fn get(&self, hash: &str) -> Result<Option<Vec<u8>>> {
        validate_hash(hash)?;
        if !self.objects.read().unwrap().contains(hash) {
            return Ok(None);
        }
        match fs::read(self.object_path(hash)) {
            Ok(b) => Ok(Some(b)),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(e.into()),
        }
    }

    pub fn exists(&self, hash: &str) -> bool {
        validate_hash(hash).map(|h| self.objects.read().unwrap().contains(h)).unwrap_or(false)
    }

    /// Snapshot of all registered object hashes (mainly for tests/inspection).
    pub fn list_objects(&self) -> Vec<String> {
        self.objects.read().unwrap().iter().cloned().collect()
    }

    // ---- upload sessions -------------------------------------------------

    /// Open a new upload session, returning its id.
    pub fn begin_upload(&self) -> String {
        use std::sync::atomic::{AtomicU64, Ordering};
        static COUNTER: AtomicU64 = AtomicU64::new(0);
        let n = COUNTER.fetch_add(1, Ordering::Relaxed);
        let id = format!(
            "up-{}-{}",
            Self::now_secs(),
            n
        );
        self.uploads.lock().unwrap().insert(
            id.clone(),
            UploadRec {
                objects: BTreeSet::new(),
                finalized_at: None,
            },
        );
        id
    }

    /// Close an upload. Its objects leave the "unfinished" set but remain
    /// protected for the configured retention period. `abort` only changes the
    /// semantic (an aborted upload is never meant to be published); retention
    /// behaviour is identical.
    pub fn finalize_upload(&self, upload_id: &str, abort: bool) -> Result<UploadSummary> {
        let mut ups = self.uploads.lock().unwrap();
        let rec = ups
            .get_mut(upload_id)
            .ok_or_else(|| Error::UploadNotFound(upload_id.to_string()))?;
        if rec.finalized_at.is_some() {
            return Err(Error::UploadStillOpen(upload_id.to_string()));
        }
        rec.finalized_at = Some(Self::now_secs());
        let objects = rec.objects.iter().cloned().collect();
        Ok(UploadSummary {
            upload_id: upload_id.to_string(),
            aborted: abort,
            objects,
        })
    }

    /// Drop records of finalized uploads whose retention period has elapsed.
    /// Their objects (if otherwise unreachable) then become collectable.
    pub fn retain_expired_uploads(&self) -> Vec<String> {
        let now = Self::now_secs();
        let cutoff = now.saturating_sub(self.upload_retention_secs);
        let mut removed = Vec::new();
        self.uploads.lock().unwrap().retain(|id, rec| match rec.finalized_at {
            None => true, // still open: always retained
            Some(ts) if ts >= cutoff => true,
            Some(_) => {
                removed.push(id.clone());
                false
            }
        });
        removed
    }

    // ---- roots -----------------------------------------------------------

    /// Publish (create or replace) the named root, pointing at `hash`.
    ///
    /// The target and every transitive dependency must already be present, so
    /// a published root can never dangle.
    pub async fn publish_root(&self, name: &str, hash: &str) -> Result<()> {
        validate_hash(hash)?;
        let _g = self.gc_lock.write().await;
        // Validate reachability/closure while holding the gc lock; a concurrent
        // GC cannot run between this check and the root insertion.
        self.validate_closure(hash)?;
        self.roots.lock().unwrap().insert(name.to_string(), hash.to_string());
        self.persist_roots()?;
        Ok(())
    }

    /// Delete a named root. Its objects become garbage unless another root
    /// still reaches them.
    pub async fn delete_root(&self, name: &str) -> Result<bool> {
        let _g = self.gc_lock.write().await;
        let existed = self.roots.lock().unwrap().remove(name).is_some();
        if existed {
            self.persist_roots()?;
        }
        Ok(existed)
    }

    pub async fn list_roots(&self) -> BTreeMap<String, String> {
        self.roots.lock().unwrap().clone()
    }

    fn persist_roots(&self) -> Result<()> {
        let roots = self.roots.lock().unwrap().clone();
        write_atomic(&self.root.join("roots.json"), &serde_json::to_vec_pretty(&roots)?)?;
        Ok(())
    }

    fn persist_kinds(&self) -> Result<()> {
        let kinds = self.kinds.read().unwrap().clone();
        write_atomic(&self.root.join("kinds.json"), &serde_json::to_vec_pretty(&kinds)?)?;
        Ok(())
    }

    /// The recorded kind of an object (defaults to block for legacy objects
    /// that predate kind metadata).
    pub fn kind_of(&self, hash: &str) -> Option<ObjectKind> {
        self.kinds.read().unwrap().get(hash).copied()
    }

    /// Confirm that `hash` and everything it references exists in the store.
    fn validate_closure(&self, hash: &str) -> Result<()> {
        let objs = self.objects.read().unwrap();
        let kinds = self.kinds.read().unwrap();
        if !objs.contains(hash) {
            return Err(Error::ObjectNotFound(hash.to_string()));
        }
        let mut stack = vec![hash.to_string()];
        let mut seen = BTreeSet::new();
        while let Some(h) = stack.pop() {
            if !seen.insert(h.clone()) {
                continue;
            }
            if !objs.contains(&h) {
                return Err(Error::MissingDependency(h));
            }
            // Only explicit manifest objects add edges.
            if kinds.get(&h) != Some(&ObjectKind::Manifest) {
                continue;
            }
            let path = self.object_path(&h);
            let bytes = match fs::read(&path) {
                Ok(b) => b,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    return Err(Error::MissingDependency(h))
                }
                Err(e) => return Err(e.into()),
            };
            stack.extend(parse_manifest(&bytes)?);
        }
        Ok(())
    }

    // ---- garbage collection ---------------------------------------------

    /// Run one mark-sweep garbage collection pass.
    ///
    /// Holds the exclusive gc lock for the whole pass, freezing root
    /// publication/deletion, and a write lock on the object index, freezing
    /// puts. The retained set is:
    ///
    /// 1. every object transitively reachable from a current root, plus
    /// 2. every object belonging to an open upload, or to an upload finalized
    ///    within the retention window.
    ///
    /// Anything else on disk is deleted.
    pub async fn gc(&self) -> Result<GcReport> {
        let _g = self.gc_lock.write().await;
        // Hold one write guard for the whole pass and use it directly: the
        // std RwLock is not re-entrant, so re-locking from this thread would
        // deadlock.
        let mut index = self.objects.write().unwrap();

        // First let finalized-but-expired upload records fall out of retention.
        self.retain_expired_uploads();

        let roots: Vec<String> = self.roots.lock().unwrap().values().cloned().collect();
        let kinds = self.kinds.read().unwrap();

        // Mark: walk references from the roots, but only along explicit
        // manifest edges; data blocks are leaves.
        let mut reachable: BTreeSet<String> = BTreeSet::new();
        let mut stack: Vec<String> = roots
            .iter()
            .filter(|h| index.contains(*h))
            .cloned()
            .collect();
        while let Some(h) = stack.pop() {
            if !reachable.insert(h.clone()) {
                continue;
            }
            if kinds.get(&h) != Some(&ObjectKind::Manifest) {
                continue;
            }
            // Only present files add edges (roots are validated, so they exist).
            let bytes = match fs::read(self.object_path(&h)) {
                Ok(b) => b,
                Err(_) => continue,
            };
            if let Ok(refs) = parse_manifest(&bytes) {
                for r in refs {
                    if index.contains(&r) {
                        stack.push(r);
                    }
                }
            }
        }

        // Retention from unfinished / recently finalized uploads.
        let (retained, open_uploads) = {
            let ups = self.uploads.lock().unwrap();
            let mut retained = BTreeSet::new();
            let mut open = 0usize;
            for rec in ups.values() {
                if rec.finalized_at.is_none() {
                    open += 1;
                }
                for o in &rec.objects {
                    retained.insert(o.clone());
                }
            }
            (retained, open)
        };

        // Sweep: every registered object not in either set is garbage.
        let snapshot: Vec<String> = index.iter().cloned().collect();
        let mut deleted = Vec::new();
        for h in snapshot {
            if reachable.contains(&h) || retained.contains(&h) {
                continue;
            }
            match fs::remove_file(self.object_path(&h)) {
                Ok(()) => {
                    index.remove(&h);
                    deleted.push(h);
                }
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                    index.remove(&h);
                }
                Err(e) => return Err(e.into()),
            }
        }

        let retained_by_upload = retained.difference(&reachable).count();
        Ok(GcReport {
            reachable: reachable.len(),
            retained_by_upload,
            deleted,
            open_uploads: open_uploads,
        })
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct UploadSummary {
    pub upload_id: String,
    pub aborted: bool,
    pub objects: Vec<String>,
}

/// Atomically write `bytes` to `dst`: write a uniquely-named temp file in the
/// same directory, then rename it over the target. A unique temp name is
/// essential because several writers persist metadata concurrently; a shared
/// fixed temp name would let one writer's rename consume another's file.
fn write_atomic(dst: &Path, bytes: &[u8]) -> Result<()> {
    let dir = dst.parent().unwrap_or_else(|| Path::new("."));
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    // Add a thread-local counter to disambiguate same-nanosecond writes.
    use std::sync::atomic::{AtomicU64, Ordering};
    static SEQ: AtomicU64 = AtomicU64::new(0);
    let tmp = dir.join(format!(".{}.{nonce}.{}.tmp",
        dst.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default(),
        SEQ.fetch_add(1, Ordering::Relaxed)));
    fs::write(&tmp, bytes)?;
    fs::rename(&tmp, dst)?;
    Ok(())
}
