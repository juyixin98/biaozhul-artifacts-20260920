//! Content-addressed object store with roots, staged uploads and persistence.
//!
//! All mutating operations are serialized through a single [`std::sync::Mutex`].
//! Garbage collection (`gc.rs`) performs mark-and-sweep inside one critical
//! section, so an object reachable from a root can never be deleted by a GC
//! running concurrently with a root publication.

use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

/// A manifest: an object that references other manifests and data blobs by
/// their content hashes.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Manifest {
    #[serde(default)]
    pub manifests: Vec<String>,
    #[serde(default)]
    pub blobs: Vec<String>,
}

impl Manifest {
    fn direct_refs(&self) -> impl Iterator<Item = &String> {
        self.manifests.iter().chain(self.blobs.iter())
    }
}

/// Server-wide defaults for staged (incomplete) uploads.
#[derive(Clone, Copy, Debug)]
pub struct StoreConfig {
    /// Default retention period in seconds for an upload that does not specify
    /// its own.
    pub default_retention_secs: u64,
}

impl Default for StoreConfig {
    fn default() -> Self {
        Self {
            default_retention_secs: 3600,
        }
    }
}

/// A staged upload as persisted on disk.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct UploadRecord {
    pub id: String,
    pub manifest: String,
    pub created_unix: u64,
    pub retention_secs: u64,
}

#[derive(Serialize, Deserialize)]
struct PersistedState {
    version: u32,
    gc_gen: u64,
    roots: HashMap<String, String>,
    /// Full object index (blobs + manifests). Kept in sync with the
    /// `objects/` directory via the same atomic state write.
    objects: Vec<String>,
    manifests: Vec<String>,
    uploads: Vec<UploadRecord>,
}

pub(crate) struct Inner {
    pub(crate) objects: HashSet<String>,
    pub(crate) manifests: HashSet<String>,
    pub(crate) roots: HashMap<String, String>,
    pub(crate) uploads: HashMap<String, UploadRecord>,
    pub(crate) gc_gen: u64,
    pub(crate) counter: u64,
}

/// Content-addressed object store. Cheap to clone (`Arc` inside).
#[derive(Clone)]
pub struct Store {
    pub(crate) inner: Arc<Mutex<Inner>>,
    pub(crate) dir: PathBuf,
    cfg: StoreConfig,
}

/// Error returned by store operations.
#[derive(Debug)]
pub enum StoreError {
    /// One or more referenced hashes do not exist.
    MissingRefs(Vec<String>),
    NotFound,
    Io(String),
    /// Upload is past its retention window and has been discarded.
    UploadExpired,
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::MissingRefs(hs) => {
                write!(f, "referenced objects do not exist: {}", hs.join(", "))
            }
            StoreError::NotFound => write!(f, "not found"),
            StoreError::Io(e) => write!(f, "io error: {e}"),
            StoreError::UploadExpired => write!(f, "upload expired before it was completed"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        StoreError::Io(e.to_string())
    }
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    hex::encode(h.finalize())
}

impl Store {
    /// Open (or create) a store backed by `dir`.
    pub fn open(dir: impl AsRef<Path>, cfg: StoreConfig) -> Result<Self, StoreError> {
        let dir = dir.as_ref().to_path_buf();
        fs::create_dir_all(dir.join("objects"))?;

        let state_path = dir.join("state.json");
        let inner = if state_path.exists() {
            let raw = fs::read(&state_path)?;
            let p: PersistedState = serde_json::from_slice(&raw)
                .map_err(|e| StoreError::Io(format!("corrupt state.json: {e}")))?;
            // Reconcile the index with what is actually on disk so a crash
            // that deleted a file cannot leave phantom entries. Files present
            // on disk but absent from the index (a crash after write, before
            // state flush) are adopted as objects; they simply become GC
            // candidates if nothing references them.
            let mut on_disk: HashSet<String> = HashSet::new();
            for entry in fs::read_dir(dir.join("objects"))? {
                if let Ok(entry) = entry {
                    if let Some(name) = entry.file_name().to_str() {
                        on_disk.insert(name.to_string());
                    }
                }
            }
            let mut objects: HashSet<String> =
                p.objects.iter().cloned().filter(|h| on_disk.contains(h)).collect();
            objects.extend(on_disk);
            let manifests: HashSet<String> = p
                .manifests
                .iter()
                .filter(|h| objects.contains(*h))
                .cloned()
                .collect();
            // Drop roots that point at manifests missing on disk.
            let roots = p
                .roots
                .into_iter()
                .filter(|(_, h)| manifests.contains(h))
                .collect();
            // Drop uploads whose manifest vanished.
            let uploads = p
                .uploads
                .into_iter()
                .filter(|u| manifests.contains(&u.manifest))
                .map(|u| (u.id.clone(), u))
                .collect();
            Inner {
                objects,
                manifests,
                roots,
                uploads,
                gc_gen: p.gc_gen,
                counter: 0,
            }
        } else {
            Inner {
                objects: HashSet::new(),
                manifests: HashSet::new(),
                roots: HashMap::new(),
                uploads: HashMap::new(),
                gc_gen: 0,
                counter: 0,
            }
        };

        let store = Store {
            inner: Arc::new(Mutex::new(inner)),
            dir,
            cfg,
        };
        store.persist()?;
        Ok(store)
    }

    pub(crate) fn object_path(&self, hash: &str) -> PathBuf {
        self.dir.join("objects").join(hash)
    }

    pub(crate) fn persist(&self) -> Result<(), StoreError> {
        let s = self.inner.lock().unwrap();
        let mut objects: Vec<&String> = s.objects.iter().collect();
        objects.sort();
        let mut manifests: Vec<&String> = s.manifests.iter().collect();
        manifests.sort();
        let state = PersistedState {
            version: 1,
            gc_gen: s.gc_gen,
            roots: s.roots.clone(),
            objects: objects.into_iter().cloned().collect(),
            manifests: manifests.into_iter().cloned().collect(),
            uploads: s.uploads.values().cloned().collect(),
        };
        // Single atomic state file (tmp + rename) keeps the index consistent
        // with the last completed mutation. Object files are content-addressed
        // and immutable, so a crash between writing an object and flushing
        // state only leaves an adoptable orphan on disk.
        let raw = serde_json::to_vec_pretty(&state).unwrap();
        let tmp = self.dir.join("state.json.tmp");
        fs::write(&tmp, raw)?;
        fs::rename(&tmp, self.dir.join("state.json"))?;
        Ok(())
    }

    pub(crate) fn now_unix() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0)
    }

    /// Current unix time (exposed for the API layer's read-only views).
    pub fn now_unix_pub() -> u64 {
        Self::now_unix()
    }

    fn next_id(&self, s: &mut Inner, prefix: &str) -> String {
        s.counter += 1;
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.subsec_nanos() as u64)
            .unwrap_or(0);
        format!("{prefix}-{:x}-{:x}", s.counter, nanos)
    }

    // ---- blobs -----------------------------------------------------------

    /// Store a raw data blob; returns its content hash. Idempotent.
    pub fn put_blob(&self, bytes: &[u8]) -> Result<String, StoreError> {
        let hash = sha256_hex(bytes);
        {
            let mut s = self.inner.lock().unwrap();
            if !s.objects.contains(&hash) {
                fs::write(self.object_path(&hash), bytes)?;
                s.objects.insert(hash.clone());
            }
        }
        self.persist()?;
        Ok(hash)
    }

    /// Fetch raw object bytes (works for both blobs and manifests).
    pub fn get_object(&self, hash: &str) -> Result<Vec<u8>, StoreError> {
        {
            let s = self.inner.lock().unwrap();
            if !s.objects.contains(hash) {
                return Err(StoreError::NotFound);
            }
        }
        fs::read(self.object_path(hash)).map_err(|_| StoreError::NotFound)
    }

    pub fn contains(&self, hash: &str) -> bool {
        self.inner.lock().unwrap().objects.contains(hash)
    }

    pub fn is_manifest(&self, hash: &str) -> bool {
        self.inner.lock().unwrap().manifests.contains(hash)
    }

    // ---- manifests -------------------------------------------------------

    /// Store a manifest after verifying that its full transitive reference
    /// closure already exists.
    pub fn put_manifest(&self, manifest: &Manifest) -> Result<String, StoreError> {
        let bytes = serde_json::to_vec(manifest).unwrap();
        self.put_manifest_bytes(&bytes)
    }

    /// Store a manifest from its exact JSON encoding (the hash covers the
    /// exact bytes the client posted).
    pub fn put_manifest_bytes(&self, bytes: &[u8]) -> Result<String, StoreError> {
        let manifest: Manifest =
            serde_json::from_slice(bytes).map_err(|e| StoreError::Io(e.to_string()))?;
        let hash = sha256_hex(bytes);

        let mut s = self.inner.lock().unwrap();
        if !s.manifests.contains(&hash) {
            let missing = self.find_missing_refs(&s, &manifest);
            if !missing.is_empty() {
                return Err(StoreError::MissingRefs(missing));
            }
            fs::write(self.object_path(&hash), bytes)?;
            s.objects.insert(hash.clone());
            s.manifests.insert(hash.clone());
        }
        drop(s);
        self.persist()?;
        Ok(hash)
    }

    /// BFS over the manifest closure; returns referenced hashes that are not
    /// present in the store.
    fn find_missing_refs(&self, s: &Inner, root: &Manifest) -> Vec<String> {
        let mut missing = Vec::new();
        let mut seen: HashSet<String> = HashSet::new();
        let mut queue: Vec<String> = root.direct_refs().cloned().collect();
        while let Some(h) = queue.pop() {
            if !seen.insert(h.clone()) {
                continue;
            }
            if !s.objects.contains(&h) {
                if !missing.contains(&h) {
                    missing.push(h);
                }
                continue;
            }
            if s.manifests.contains(&h) {
                if let Ok(bytes) = fs::read(self.object_path(&h)) {
                    if let Ok(m) = serde_json::from_slice::<Manifest>(&bytes) {
                        queue.extend(m.direct_refs().cloned());
                    }
                }
            }
        }
        missing.sort();
        missing
    }

    // ---- roots -----------------------------------------------------------

    /// Publish (or repoint) a named root at a manifest. The existence check
    /// and the insert happen in one critical section, so GC cannot reclaim the
    /// target between them.
    pub fn set_root(&self, id: &str, manifest_hash: &str) -> Result<(), StoreError> {
        {
            let mut s = self.inner.lock().unwrap();
            if !s.manifests.contains(manifest_hash) {
                return Err(StoreError::NotFound);
            }
            s.roots.insert(id.to_string(), manifest_hash.to_string());
        }
        self.persist()?;
        Ok(())
    }

    pub fn delete_root(&self, id: &str) -> Result<bool, StoreError> {
        let existed = {
            let mut s = self.inner.lock().unwrap();
            s.roots.remove(id).is_some()
        };
        self.persist()?;
        Ok(existed)
    }

    pub fn roots(&self) -> HashMap<String, String> {
        self.inner.lock().unwrap().roots.clone()
    }

    // ---- staged uploads --------------------------------------------------

    /// Stage an incomplete upload for `manifest`. While the upload is live and
    /// within its retention window, GC protects its entire reference closure.
    pub fn start_upload(
        &self,
        manifest: &Manifest,
        retention_secs: Option<u64>,
    ) -> Result<UploadRecord, StoreError> {
        let bytes = serde_json::to_vec(manifest).unwrap();
        let manifest_hash = sha256_hex(&bytes);
        let mut s = self.inner.lock().unwrap();
        if !s.manifests.contains(&manifest_hash) {
            let missing = self.find_missing_refs(&s, manifest);
            if !missing.is_empty() {
                return Err(StoreError::MissingRefs(missing));
            }
            fs::write(self.object_path(&manifest_hash), &bytes)?;
            s.objects.insert(manifest_hash.clone());
            s.manifests.insert(manifest_hash.clone());
        }
        let rec = UploadRecord {
            id: self.next_id(&mut s, "up"),
            manifest: manifest_hash,
            created_unix: Self::now_unix(),
            retention_secs: retention_secs.unwrap_or(self.cfg.default_retention_secs),
        };
        s.uploads.insert(rec.id.clone(), rec.clone());
        drop(s);
        self.persist()?;
        Ok(rec)
    }

    /// Complete an upload. If `root` is given, its manifest is published under
    /// that root id; protection by the staging window ends either way.
    /// Fails with [`StoreError::UploadExpired`] if the retention window has
    /// elapsed and GC may already have reclaimed the manifest — completing
    /// then would publish a dangling root.
    pub fn complete_upload(
        &self,
        upload_id: &str,
        root: Option<&str>,
    ) -> Result<String, StoreError> {
        let manifest = {
            let mut s = self.inner.lock().unwrap();
            let rec = s
                .uploads
                .get(upload_id)
                .ok_or(StoreError::NotFound)?
                .clone();
            let now = Self::now_unix();
            let expired = now >= rec.created_unix.saturating_add(rec.retention_secs);
            if expired || !s.manifests.contains(&rec.manifest) {
                return Err(StoreError::UploadExpired);
            }
            s.uploads.remove(upload_id);
            if let Some(root) = root {
                s.roots.insert(root.to_string(), rec.manifest.clone());
            }
            rec.manifest
        };
        self.persist()?;
        Ok(manifest)
    }

    pub fn abort_upload(&self, upload_id: &str) -> Result<(), StoreError> {
        {
            let mut s = self.inner.lock().unwrap();
            s.uploads.remove(upload_id).ok_or(StoreError::NotFound)?;
        }
        self.persist()?;
        Ok(())
    }

    pub fn uploads(&self) -> Vec<UploadRecord> {
        self.inner.lock().unwrap().uploads.values().cloned().collect()
    }

    // ---- inspection ------------------------------------------------------

    pub fn gc_generation(&self) -> u64 {
        self.inner.lock().unwrap().gc_gen
    }

    pub fn object_count(&self) -> usize {
        self.inner.lock().unwrap().objects.len()
    }

    /// Transitive closure of hashes reachable from every current root and
    /// every non-expired staged upload.
    pub fn reachable_closure(&self) -> HashSet<String> {
        let s = self.inner.lock().unwrap();
        let now = Self::now_unix();
        let seeds: Vec<String> = s
            .roots
            .values()
            .cloned()
            .chain(
                s.uploads
                    .values()
                    .filter(|u| now < u.created_unix.saturating_add(u.retention_secs))
                    .map(|u| u.manifest.clone()),
            )
            .collect();
        let mut reachable = HashSet::new();
        let mut stack = seeds;
        while let Some(h) = stack.pop() {
            if !reachable.insert(h.clone()) {
                continue;
            }
            if s.manifests.contains(&h) {
                if let Ok(bytes) = fs::read(self.object_path(&h)) {
                    if let Ok(m) = serde_json::from_slice::<Manifest>(&bytes) {
                        stack.extend(m.direct_refs().cloned());
                    }
                }
            }
        }
        reachable
    }
}
