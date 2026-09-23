//! Page-level copy-on-write snapshot storage engine.
//!
//! Layout on disk:
//!   <dir>/pages/<id>        immutable page files (PAGE_SIZE bytes)
//!   <dir>/snapshots/<name>.json   snapshot manifests (logical page -> physical page id)
//!   <dir>/meta.json         cumulative statistics (page allocations)
//!
//! Crash-safety protocol:
//!   1. A new physical page is fully written, fsynced and atomically renamed
//!      into pages/ BEFORE any manifest references it.
//!   2. A manifest (the "root pointer" of a snapshot) is committed by writing
//!      a temp file, fsyncing it, then atomically rename(2) over the old one,
//!      followed by an fsync of the containing directory.
//!   3. A crash may leave orphan page files (written but never referenced).
//!      They are reclaimed by mark-and-sweep GC at startup and on demand.
//!   Branching never copies page data: it clones the page table and bumps the
//!   shared usage implicitly (liveness is computed by GC from all manifests).

use crate::fail;

use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub const PAGE_SIZE: usize = 4096;

#[derive(Debug)]
pub enum StoreError {
    SnapshotNotFound(String),
    SnapshotExists(String),
    PageNotFound { snapshot: String, index: u64 },
    PageTooLarge { max: usize, got: usize },
    InvalidName(String),
    Io(io::Error),
    Corrupt(String),
}

impl fmt::Display for StoreError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::SnapshotNotFound(n) => write!(f, "snapshot not found: {n}"),
            Self::SnapshotExists(n) => write!(f, "snapshot already exists: {n}"),
            Self::PageNotFound { snapshot, index } => {
                write!(f, "page {index} not present in snapshot {snapshot}")
            }
            Self::PageTooLarge { max, got } => {
                write!(f, "page payload too large: got {got} bytes, max {max}")
            }
            Self::InvalidName(n) => write!(f, "invalid snapshot name: {n:?}"),
            Self::Io(e) => write!(f, "io error: {e}"),
            Self::Corrupt(m) => write!(f, "corrupt state: {m}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<io::Error> for StoreError {
    fn from(e: io::Error) -> Self {
        Self::Io(e)
    }
}

pub type Result<T> = std::result::Result<T, StoreError>;

/// One entry of a snapshot page table: physical page id + logical length.
#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
pub struct PageRef {
    pub id: u64,
    pub len: u32,
}

#[derive(Debug, Serialize, Deserialize)]
struct Manifest {
    name: String,
    pages: Vec<Option<PageRef>>,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Meta {
    /// Cumulative number of physical pages ever allocated (i.e. COW copies).
    pages_allocated: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct SnapshotInfo {
    pub name: String,
    /// Highest logical page index + 1 (size of the page table).
    pub logical_pages: usize,
    /// Number of present (non-hole) logical pages.
    pub present_pages: usize,
    /// Physical pages referenced only by this snapshot.
    pub private_pages: usize,
    /// Physical pages referenced by this snapshot and at least one other.
    pub shared_pages: usize,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub snapshots: usize,
    /// Physical page files currently on disk.
    pub page_files: usize,
    /// Pages referenced by at least one snapshot (the live set).
    pub live_pages: usize,
    /// Pages on disk referenced by no snapshot (awaiting GC).
    pub orphan_pages: usize,
    /// Cumulative physical page allocations since the store was created.
    /// Every COW write allocates exactly one page; branching allocates none.
    pub pages_allocated: u64,
}

struct State {
    next_page_id: u64,
    snapshots: BTreeMap<String, Vec<Option<PageRef>>>,
    meta: Meta,
}

pub struct Store {
    dir: PathBuf,
    state: Mutex<State>,
}

fn fsync_dir(dir: &Path) -> io::Result<()> {
    fs::File::open(dir)?.sync_all()
}

/// Atomically persist `bytes` to `path` via tmp-file + fsync + rename + dir fsync.

fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let tmp = path.with_extension("tmp");
    {
        let mut f = fs::File::create(&tmp)?;
        use io::Write;
        f.write_all(bytes)?;
        f.sync_all()?;
    }
    fail::point("commit_tmp_written");
    fs::rename(&tmp, path)?;
    if let Some(parent) = path.parent() {
        fsync_dir(parent)?;
    }
    Ok(())
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 128
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_' || c == '.')
        && !name.starts_with('.')
}

impl Store {
    /// Open (or create) a store rooted at `dir`. Recovers from any prior crash:
    /// loads all committed manifests and sweeps orphan pages.
    pub fn open(dir: impl AsRef<Path>) -> Result<Store> {
        let dir = dir.as_ref().to_path_buf();
        fs::create_dir_all(dir.join("pages"))?;
        fs::create_dir_all(dir.join("snapshots"))?;

        let meta: Meta = match fs::read(dir.join("meta.json")) {
            Ok(b) => serde_json::from_slice(&b)
                .map_err(|e| StoreError::Corrupt(format!("meta.json: {e}")))?,
            Err(e) if e.kind() == io::ErrorKind::NotFound => Meta::default(),
            Err(e) => return Err(e.into()),
        };

        let mut snapshots = BTreeMap::new();
        for entry in fs::read_dir(dir.join("snapshots"))? {
            let path = entry?.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") {
                continue; // ignore stray tmp files from crashed commits
            }
            let bytes = fs::read(&path)?;
            let m: Manifest = serde_json::from_slice(&bytes).map_err(|e| {
                StoreError::Corrupt(format!("{}: {e}", path.display()))
            })?;
            snapshots.insert(m.name.clone(), m.pages);
        }

        let mut max_id = 0u64;
        for entry in fs::read_dir(dir.join("pages"))? {
            let name = entry?.file_name();
            if let Some(s) = name.to_str() {
                if let Ok(id) = s.parse::<u64>() {
                    max_id = max_id.max(id);
                }
            }
        }

        let store = Store {
            dir,
            state: Mutex::new(State {
                next_page_id: max_id + 1,
                snapshots,
                meta,
            }),
        };
        // Reclaim pages orphaned by a crash (written but never committed,
        // or belonging to a deleted snapshot).
        store.gc()?;
        Ok(store)
    }

    fn pages_dir(&self) -> PathBuf {
        self.dir.join("pages")
    }

    fn manifest_path(&self, name: &str) -> PathBuf {
        self.dir.join("snapshots").join(format!("{name}.json"))
    }

    fn page_path(&self, id: u64) -> PathBuf {
        self.pages_dir().join(id.to_string())
    }

    fn persist_manifest(&self, name: &str, pages: &[Option<PageRef>]) -> Result<()> {
        let m = Manifest {
            name: name.to_string(),
            pages: pages.to_vec(),
        };
        let bytes = serde_json::to_vec(&m).expect("manifest serialization");
        atomic_write(&self.manifest_path(name), &bytes)?;
        Ok(())
    }

    fn persist_meta(&self, meta: &Meta) -> Result<()> {
        let bytes = serde_json::to_vec(meta).expect("meta serialization");
        atomic_write(&self.dir.join("meta.json"), &bytes)?;
        Ok(())
    }

    /// Create a new snapshot. With `from`, the new snapshot branches off the
    /// source: it shares all physical pages (no data is copied).
    pub fn create_snapshot(&self, name: &str, from: Option<&str>) -> Result<()> {
        if !valid_name(name) {
            return Err(StoreError::InvalidName(name.into()));
        }
        let mut st = self.state.lock().unwrap();
        if st.snapshots.contains_key(name) {
            return Err(StoreError::SnapshotExists(name.into()));
        }
        let pages = match from {
            Some(src) => st
                .snapshots
                .get(src)
                .ok_or_else(|| StoreError::SnapshotNotFound(src.into()))?
                .clone(),
            None => Vec::new(),
        };
        self.persist_manifest(name, &pages)?;
        fail::point("branch_committed");
        st.snapshots.insert(name.to_string(), pages);
        Ok(())
    }

    /// Delete a snapshot. Its pages stay on disk until GC; shared pages are
    /// untouched because liveness is derived from the remaining manifests.
    pub fn delete_snapshot(&self, name: &str) -> Result<()> {
        let mut st = self.state.lock().unwrap();
        if st.snapshots.remove(name).is_none() {
            return Err(StoreError::SnapshotNotFound(name.into()));
        }
        let path = self.manifest_path(name);
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(e) if e.kind() == io::ErrorKind::NotFound => {}
            Err(e) => return Err(e.into()),
        }
        fsync_dir(&self.dir.join("snapshots"))?;
        fail::point("delete_committed");
        Ok(())
    }

    /// Write one logical page. Always copy-on-write: allocates a fresh
    /// physical page; the old page (possibly shared) is never modified.
    pub fn write_page(&self, name: &str, index: u64, data: &[u8]) -> Result<()> {
        if data.len() > PAGE_SIZE {
            return Err(StoreError::PageTooLarge {
                max: PAGE_SIZE,
                got: data.len(),
            });
        }
        let mut st = self.state.lock().unwrap();
        if !st.snapshots.contains_key(name) {
            return Err(StoreError::SnapshotNotFound(name.into()));
        }

        // 1. Allocate and durably persist the new physical page first.
        let id = st.next_page_id;
        st.next_page_id += 1;
        let mut buf = vec![0u8; PAGE_SIZE];
        buf[..data.len()].copy_from_slice(data);
        let tmp = self.pages_dir().join(format!(".{id}.tmp"));
        {
            use io::Write;
            let mut f = fs::File::create(&tmp)?;
            f.write_all(&buf)?;
            f.sync_all()?;
        }
        fail::point("page_flushed");
        fs::rename(&tmp, self.page_path(id))?;
        fsync_dir(&self.pages_dir())?;
        fail::point("page_committed");

        // 2. Publish: update the page table and commit the manifest atomically.
        {
            let table = st.snapshots.get_mut(name).expect("checked above");
            let idx = index as usize;
            if idx >= table.len() {
                table.resize(idx + 1, None);
            }
            table[idx] = Some(PageRef {
                id,
                len: data.len() as u32,
            });
        }
        st.meta.pages_allocated += 1;
        let table = st.snapshots.get(name).expect("checked above");
        self.persist_manifest(name, table)?;
        fail::point("manifest_committed");
        self.persist_meta(&st.meta)?;
        Ok(())
    }

    /// Read one logical page, returning its exact original bytes.
    pub fn read_page(&self, name: &str, index: u64) -> Result<Vec<u8>> {
        let st = self.state.lock().unwrap();
        let table = st
            .snapshots
            .get(name)
            .ok_or_else(|| StoreError::SnapshotNotFound(name.into()))?;
        let pref = table
            .get(index as usize)
            .copied()
            .flatten()
            .ok_or_else(|| StoreError::PageNotFound {
                snapshot: name.into(),
                index,
            })?;
        let bytes = fs::read(self.page_path(pref.id)).map_err(|e| {
            if e.kind() == io::ErrorKind::NotFound {
                StoreError::Corrupt(format!("manifest references missing page {}", pref.id))
            } else {
                StoreError::Io(e)
            }
        })?;
        Ok(bytes[..pref.len as usize].to_vec())
    }

    pub fn list_snapshots(&self) -> Vec<String> {
        self.state.lock().unwrap().snapshots.keys().cloned().collect()
    }

    fn live_set(st: &State) -> BTreeSet<u64> {
        st.snapshots
            .values()
            .flat_map(|t| t.iter().flatten().map(|p| p.id))
            .collect()
    }

    pub fn snapshot_info(&self, name: &str) -> Result<SnapshotInfo> {
        let st = self.state.lock().unwrap();
        let table = st
            .snapshots
            .get(name)
            .ok_or_else(|| StoreError::SnapshotNotFound(name.into()))?;
        // Count references across all snapshots to classify shared vs private.
        let mut refcount: BTreeMap<u64, usize> = BTreeMap::new();
        for t in st.snapshots.values() {
            for p in t.iter().flatten() {
                *refcount.entry(p.id).or_insert(0) += 1;
            }
        }
        let mut private_pages = 0;
        let mut shared_pages = 0;
        for p in table.iter().flatten() {
            if refcount.get(&p.id).copied().unwrap_or(0) > 1 {
                shared_pages += 1;
            } else {
                private_pages += 1;
            }
        }
        Ok(SnapshotInfo {
            name: name.into(),
            logical_pages: table.len(),
            present_pages: table.iter().flatten().count(),
            private_pages,
            shared_pages,
        })
    }

    pub fn stats(&self) -> Result<Stats> {
        let st = self.state.lock().unwrap();
        let live = Self::live_set(&st);
        let mut page_files = 0usize;
        for entry in fs::read_dir(self.pages_dir())? {
            let name = entry?.file_name();
            if name.to_str().is_some_and(|s| s.parse::<u64>().is_ok()) {
                page_files += 1;
            }
        }
        Ok(Stats {
            snapshots: st.snapshots.len(),
            page_files,
            live_pages: live.len(),
            orphan_pages: page_files.saturating_sub(live.len()),
            pages_allocated: st.meta.pages_allocated,
        })
    }

    /// Mark-and-sweep GC: delete page files not referenced by any manifest.
    /// Returns the ids removed.
    pub fn gc(&self) -> Result<Vec<u64>> {
        let st = self.state.lock().unwrap();
        let live = Self::live_set(&st);
        let mut removed = Vec::new();
        for entry in fs::read_dir(self.pages_dir())? {
            let entry = entry?;
            let name = entry.file_name();
            let Some(s) = name.to_str() else { continue };
            // Also clean leftover temp files from crashed page writes.
            if s.starts_with('.') && s.ends_with(".tmp") {
                let _ = fs::remove_file(entry.path());
                continue;
            }
            if let Ok(id) = s.parse::<u64>() {
                if !live.contains(&id) {
                    fs::remove_file(self.page_path(id))?;
                    removed.push(id);
                }
            }
        }
        if !removed.is_empty() {
            fsync_dir(&self.pages_dir())?;
        }
        Ok(removed)
    }
}
