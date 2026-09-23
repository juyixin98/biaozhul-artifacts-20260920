//! Page-level copy-on-write storage engine.
//!
//! On-disk layout under the data directory:
//! - `MANIFEST`         : JSON snapshot table (branch name -> root page id, parent, ...)
//! - `ALLOC`            : high-water mark of minted page ids (persisted before a page is written)
//! - `data/<pid>.page`  : immutable page file
//!                        leaf page = raw user bytes; root page = `slots` little-endian u64s
//! - `refcounts/<pid>.rc`: ASCII reference count (number of committed root pages referencing it)
//! - `CRASH`            : optional fault-injection directive (`<point>` or `<point>:<nth>`)
//!
//! Reference counting invariant (leaf pages):
//!   rc(pid) == number of leaf slots, across every committed root page, equal to pid.
//! Root pages are never shared between branches (branching copies the root), so their rc is 1.
//!
//! Write-one-page commit protocol (crash safe, copies exactly 2 pages):
//!   1. mint leaf id (allocator fsynced first, so an id is never reused)
//!   2. write new leaf page: temp file + fsync + rename + fsync(dir); rc = 1
//!   3. mint root id, write a NEW root naming every untouched leaf (shared) plus the new leaf
//!   4. atomically replace MANIFEST (temp + fsync + rename + fsync dir)  <-- commit point
//!   5. unlink the old root page; decrement the replaced leaf (free at rc 0)
//! Untouched leaves keep the exact same reference count across the root swap, so step 3 copies
//! only root metadata and never touches their data: a write costs 2 page copies regardless of
//! how many pages a snapshot holds.
//!
//! Branching copies one root page and bumps shared-leaf refcounts; leaf data is never copied.
//! Any crash leaves only orphans / stale counts; `recover()` (run on every open) rebuilds
//! counts by walking committed roots and removes unreachable pages.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::io::Write;
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};

pub const PAGE_SIZE: usize = 4096;
const DEFAULT_SLOTS: usize = 64;
pub const CRASH_EXIT_CODE: i32 = 37;

const MANIFEST: &str = "MANIFEST";
const ALLOC: &str = "ALLOC";
const CRASH: &str = "CRASH";
const DATA_DIR: &str = "data";
const RC_DIR: &str = "refcounts";

#[derive(Debug)]
pub enum StoreError {
    Io(std::io::Error),
    NotFound(String),
    Conflict(String),
    BadRequest(String),
    Corrupt(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Io(e) => write!(f, "io error: {e}"),
            StoreError::NotFound(s) => write!(f, "not found: {s}"),
            StoreError::Conflict(s) => write!(f, "conflict: {s}"),
            StoreError::BadRequest(s) => write!(f, "bad request: {s}"),
            StoreError::Corrupt(s) => write!(f, "corrupt store: {s}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<std::io::Error> for StoreError {
    fn from(e: std::io::Error) -> Self {
        StoreError::Io(e)
    }
}

impl From<serde_json::Error> for StoreError {
    fn from(e: serde_json::Error) -> Self {
        StoreError::Corrupt(format!("json: {e}"))
    }
}

pub type Result<T> = std::result::Result<T, StoreError>;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BranchRec {
    pub root: u64,
    #[serde(default)]
    pub parent: Option<String>,
    #[serde(default)]
    pub created_at: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
struct Manifest {
    version: u32,
    page_size: usize,
    slots: usize,
    next_id: u64,
    branches: BTreeMap<String, BranchRec>,
}

#[derive(Debug, Clone, Serialize)]
pub struct BranchInfo {
    pub name: String,
    pub parent: Option<String>,
    pub root: u64,
    pub pages: usize,
    pub created_at: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct WriteOutcome {
    pub operation: String,
    pub branch: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub index: Option<usize>,
    pub page_id: u64,
    pub root: u64,
    /// physical pages copied by this operation (1 for branch, 2 for page write)
    pub copied_pages: u64,
}

#[derive(Debug, Clone, Serialize)]
pub struct RecoveryInfo {
    pub orphans_removed: u64,
    pub live_pages: Vec<u64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct Stats {
    pub page_size: usize,
    pub slots: usize,
    pub next_id: u64,
    /// page ids ever minted (each mint is one physical page copy), incl. ids burned by crashes
    pub pages_minted_total: u64,
    /// page ids reachable from a committed snapshot right now
    pub live_pages: Vec<u64>,
    pub live_page_count: usize,
    pub refcounts: BTreeMap<u64, u32>,
    pub branches: Vec<BranchInfo>,
}

struct Inner {
    root: PathBuf,
    next_id: u64,
    slots_n: usize,
    branches: BTreeMap<String, BranchRec>,
    branch_slots: BTreeMap<String, Vec<u64>>,
    fault_hits: HashMap<String, u64>,
    tmp_seq: u64,
    kill: bool,
}

/// Thread-safe handle to the store.
pub struct Store {
    inner: Mutex<Inner>,
}

impl Store {
    pub fn open(root: impl AsRef<Path>) -> Result<Store> {
        let root = root.as_ref().to_path_buf();
        fs::create_dir_all(root.join(DATA_DIR))?;
        fs::create_dir_all(root.join(RC_DIR))?;

        let man_path = root.join(MANIFEST);
        let mut inner = if man_path.exists() {
            let bytes = fs::read(&man_path)?;
            let m: Manifest = serde_json::from_slice(&bytes)
                .map_err(|e| StoreError::Corrupt(format!("manifest parse: {e}")))?;
            if m.version != 1 {
                return Err(StoreError::Corrupt(format!(
                    "manifest version {}",
                    m.version
                )));
            }
            if m.page_size != PAGE_SIZE {
                return Err(StoreError::Corrupt(format!(
                    "page size mismatch: {} != {PAGE_SIZE}",
                    m.page_size
                )));
            }
            let mut branch_slots = BTreeMap::new();
            for (name, b) in &m.branches {
                let slots = if b.root == 0 {
                    vec![0u64; m.slots]
                } else {
                    read_root_slots(&root.join(DATA_DIR).join(page_name(b.root)), m.slots)?
                };
                branch_slots.insert(name.clone(), slots);
            }
            Inner {
                root,
                next_id: m.next_id,
                slots_n: m.slots,
                branches: m.branches,
                branch_slots,
                fault_hits: HashMap::new(),
                tmp_seq: 0,
                kill: std::env::var("COW_CRASH_KILL")
                    .map(|v| v == "1")
                    .unwrap_or(false),
            }
        } else {
            let fresh = Inner {
                root,
                next_id: 1,
                slots_n: DEFAULT_SLOTS,
                branches: BTreeMap::new(),
                branch_slots: BTreeMap::new(),
                fault_hits: HashMap::new(),
                tmp_seq: 0,
                kill: std::env::var("COW_CRASH_KILL")
                    .map(|v| v == "1")
                    .unwrap_or(false),
            };
            fresh.persist_alloc()?;
            fresh.persist_manifest()?;
            fresh
        };

        if let Ok(s) = fs::read_to_string(inner.root.join(ALLOC)) {
            if let Ok(v) = s.trim().parse::<u64>() {
                inner.next_id = inner.next_id.max(v);
            }
        }

        inner.recover()?;
        Ok(Store {
            inner: Mutex::new(inner),
        })
    }

    pub fn list_branches(&self) -> Vec<BranchInfo> {
        self.inner.lock().unwrap().branch_infos()
    }

    pub fn stats(&self) -> Result<Stats> {
        self.inner.lock().unwrap().stats()
    }

    pub fn create_branch(&self, name: &str, parent: Option<&str>) -> Result<WriteOutcome> {
        self.inner.lock().unwrap().create_branch(name, parent)
    }

    pub fn delete_branch(&self, name: &str) -> Result<()> {
        self.inner.lock().unwrap().delete_branch(name)
    }

    pub fn read_page(&self, branch: &str, index: usize) -> Result<Option<Vec<u8>>> {
        self.inner.lock().unwrap().read_page(branch, index)
    }

    /// Physical page id backing slot `index` (0 == empty), for verification/stats.
    pub fn page_id_at(&self, branch: &str, index: usize) -> Result<u64> {
        let g = self.inner.lock().unwrap();
        g.require_branch(branch)?;
        if index >= g.slots_n {
            return Err(StoreError::BadRequest(format!(
                "index {index} >= {}",
                g.slots_n
            )));
        }
        Ok(g.branch_slots[branch][index])
    }

    pub fn slots(&self) -> usize {
        self.inner.lock().unwrap().slots_n
    }

    pub fn write_page(&self, branch: &str, index: usize, data: &[u8]) -> Result<WriteOutcome> {
        self.inner.lock().unwrap().write_page(branch, index, data)
    }

    pub fn set_fault(&self, directive: Option<&str>) -> Result<()> {
        let g = self.inner.lock().unwrap();
        match directive {
            None | Some("") => {
                let _ = fs::remove_file(g.root.join(CRASH));
            }
            Some(d) => {
                fs::write(g.root.join(CRASH), d)?;
            }
        }
        Ok(())
    }

    pub fn get_fault(&self) -> Result<Option<String>> {
        let p = self.inner.lock().unwrap().root.join(CRASH);
        if p.exists() {
            Ok(Some(fs::read_to_string(p)?.trim().to_string()))
        } else {
            Ok(None)
        }
    }
}

impl Inner {
    // ---------- crash injection ----------

    fn crash(&mut self, point: &str) {
        let Ok(raw) = fs::read_to_string(self.root.join(CRASH)) else {
            return;
        };
        let directive = raw.trim();
        if directive.is_empty() {
            return;
        }
        let (want, nth) = match directive.split_once(':') {
            Some((p, n)) => (p, n.parse::<u64>().ok()),
            None => (directive, None),
        };
        if want != point {
            return;
        }
        let hits = self.fault_hits.entry(point.to_string()).or_insert(0);
        *hits += 1;
        let fire = nth.map(|n| *hits == n).unwrap_or(true);
        if fire {
            if self.kill {
                // immediate process death: only previously fsynced state survives
                std::process::exit(CRASH_EXIT_CODE);
            } else {
                panic!("injected fault at {point}");
            }
        }
    }

    // ---------- durability primitives ----------

    fn tmp_path(&mut self, dir: &Path) -> PathBuf {
        self.tmp_seq += 1;
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        dir.join(format!(
            ".tmp-{}-{}-{}",
            std::process::id(),
            nanos,
            self.tmp_seq
        ))
    }

    /// write-to-temp, fsync, atomic rename, fsync directory
    fn atomic_write(&mut self, path: &Path, bytes: &[u8]) -> Result<()> {
        let dir = path.parent().unwrap();
        let tmp = self.tmp_path(dir);
        {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&tmp)?;
            f.write_all(bytes)?;
            f.sync_all()?;
        }
        fs::rename(&tmp, path)?;
        fsync_dir(dir)?;
        Ok(())
    }

    fn alloc_id(&mut self) -> Result<u64> {
        let id = self.next_id;
        self.next_id += 1;
        self.persist_alloc()?;
        Ok(id)
    }

    fn persist_alloc(&self) -> Result<()> {
        let tmp = self.root.join(format!(".{ALLOC}.tmp"));
        {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&tmp)?;
            writeln!(f, "{}", self.next_id)?;
            f.sync_all()?;
        }
        fs::rename(&tmp, self.root.join(ALLOC))?;
        fsync_dir(&self.root)?;
        Ok(())
    }

    fn persist_manifest(&self) -> Result<()> {
        let m = Manifest {
            version: 1,
            page_size: PAGE_SIZE,
            slots: self.slots_n,
            next_id: self.next_id,
            branches: self.branches.clone(),
        };
        let bytes = serde_json::to_vec_pretty(&m)?;
        let tmp = self.root.join(format!(".{MANIFEST}.tmp"));
        {
            let mut f = fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(true)
                .mode(0o600)
                .open(&tmp)?;
            f.write_all(&bytes)?;
            f.sync_all()?;
        }
        fs::rename(&tmp, self.root.join(MANIFEST))?;
        fsync_dir(&self.root)?;
        Ok(())
    }

    fn rc_path(&self, pid: u64) -> PathBuf {
        self.root.join(RC_DIR).join(format!("{pid}.rc"))
    }

    fn page_path(&self, pid: u64) -> PathBuf {
        self.root.join(DATA_DIR).join(page_name(pid))
    }

    fn set_rc(&mut self, pid: u64, v: u32) -> Result<()> {
        if v == 0 {
            let _ = fs::remove_file(self.rc_path(pid));
            fsync_dir(&self.root.join(RC_DIR))?;
        } else {
            self.atomic_write(&self.rc_path(pid), v.to_string().as_bytes())?;
        }
        Ok(())
    }

    /// Missing count file reads as 0 (only expected for fully unreferenced pages).
    fn get_rc(&self, pid: u64) -> u32 {
        fs::read_to_string(self.rc_path(pid))
            .ok()
            .and_then(|s| s.trim().parse::<u32>().ok())
            .unwrap_or(0)
    }

    // ---------- operations ----------

    fn branch_infos(&self) -> Vec<BranchInfo> {
        self.branches
            .iter()
            .map(|(name, b)| BranchInfo {
                name: name.clone(),
                parent: b.parent.clone(),
                root: b.root,
                pages: self
                    .branch_slots
                    .get(name)
                    .map(|s| s.iter().filter(|p| **p != 0).count())
                    .unwrap_or(0),
                created_at: b.created_at,
            })
            .collect()
    }

    fn require_branch(&self, name: &str) -> Result<()> {
        if !self.branches.contains_key(name) {
            Err(StoreError::NotFound(format!("branch {name}")))
        } else {
            Ok(())
        }
    }

    fn create_branch(&mut self, name: &str, parent: Option<&str>) -> Result<WriteOutcome> {
        validate_name(name)?;
        if self.branches.contains_key(name) {
            return Err(StoreError::Conflict(format!(
                "branch {name} already exists"
            )));
        }
        let parent_slots = match parent {
            None => {
                if !self.branches.is_empty() {
                    return Err(StoreError::BadRequest(
                        "parent is required when other branches already exist".into(),
                    ));
                }
                vec![0u64; self.slots_n]
            }
            Some(p) => {
                self.require_branch(p)?;
                self.branch_slots[p].clone()
            }
        };

        // one metadata-only page copy: the new branch's private root page
        let root_id = self.alloc_id()?;
        self.crash("before_root");
        self.atomic_write(&self.page_path(root_id), &encode_slots(&parent_slots))?;
        self.crash("after_root");
        self.crash("before_rootrc");
        self.set_rc(root_id, 1)?;
        self.crash("after_rootrc");

        // share every live leaf: bump its refcount; leaf data itself is never copied
        for pid in parent_slots.iter().copied().filter(|p| *p != 0) {
            self.crash("before_share");
            let v = self.get_rc(pid);
            self.set_rc(pid, v + 1)?;
            self.crash("after_share");
        }

        self.crash("before_manifest");
        self.branches.insert(
            name.to_string(),
            BranchRec {
                root: root_id,
                parent: parent.map(|s| s.to_string()),
                created_at: now_secs(),
            },
        );
        self.branch_slots.insert(name.to_string(), parent_slots);
        self.persist_manifest()?;
        self.crash("after_manifest");

        Ok(WriteOutcome {
            operation: "branch".into(),
            branch: name.to_string(),
            index: None,
            page_id: root_id,
            root: root_id,
            copied_pages: 1,
        })
    }

    fn delete_branch(&mut self, name: &str) -> Result<()> {
        self.require_branch(name)?;
        let old_root = self.branches[name].root;
        let old_slots = self.branch_slots[name].clone();

        // commit point: branch disappears from the snapshot table
        self.crash("before_manifest_del");
        self.branches.remove(name);
        self.branch_slots.remove(name);
        self.persist_manifest()?;
        self.crash("after_manifest_del");

        // then release its root and the leaves only it still pinned
        if old_root != 0 {
            self.crash("before_dec");
            self.release_root_tree(old_root, &old_slots)?;
        }
        Ok(())
    }

    fn read_page(&self, branch: &str, index: usize) -> Result<Option<Vec<u8>>> {
        self.require_branch(branch)?;
        if index >= self.slots_n {
            return Err(StoreError::BadRequest(format!(
                "index {index} >= {}",
                self.slots_n
            )));
        }
        let pid = self.branch_slots[branch][index];
        if pid == 0 {
            return Ok(None);
        }
        Ok(Some(fs::read(self.page_path(pid))?))
    }

    fn write_page(&mut self, branch: &str, index: usize, data: &[u8]) -> Result<WriteOutcome> {
        self.require_branch(branch)?;
        if index >= self.slots_n {
            return Err(StoreError::BadRequest(format!(
                "index {index} >= {}",
                self.slots_n
            )));
        }
        if data.is_empty() || data.len() > PAGE_SIZE {
            return Err(StoreError::BadRequest(format!(
                "page must be 1..={PAGE_SIZE} bytes, got {}",
                data.len()
            )));
        }

        let old_slots = self.branch_slots[branch].clone();
        let old_pid = old_slots[index];
        let old_root = self.branches[branch].root;

        // 1. new leaf page (copy #1)
        let leaf_id = self.alloc_id()?;
        self.crash("before_page");
        self.atomic_write(&self.page_path(leaf_id), data)?;
        self.crash("after_page");
        self.crash("before_setrc");
        self.set_rc(leaf_id, 1)?;
        self.crash("after_setrc");

        // 2. new root page naming all untouched leaves + the new one (copy #2, metadata only)
        let mut new_slots = old_slots;
        new_slots[index] = leaf_id;
        let root_id = self.alloc_id()?;
        self.crash("before_root");
        self.atomic_write(&self.page_path(root_id), &encode_slots(&new_slots))?;
        self.crash("after_root");
        self.crash("before_rootrc");
        self.set_rc(root_id, 1)?;
        self.crash("after_rootrc");

        // 3. commit: publish the new root pointer. Untouched leaves are referenced exactly once
        //    by the new root just as they were by the old root, so their counts do not change.
        self.crash("before_manifest");
        if let Some(b) = self.branches.get_mut(branch) {
            b.root = root_id;
        }
        self.branch_slots.insert(branch.to_string(), new_slots);
        self.persist_manifest()?;
        self.crash("after_manifest");

        // 4. post-commit cleanup: old root is now unreferenced (roots are never shared);
        //    only the replaced leaf loses a reference.
        if old_root != 0 {
            self.drop_root_page(old_root)?;
        }
        if old_pid != 0 {
            self.dec_leaf(old_pid)?;
        }

        Ok(WriteOutcome {
            operation: "write".into(),
            branch: branch.to_string(),
            index: Some(index),
            page_id: leaf_id,
            root: root_id,
            copied_pages: 2,
        })
    }

    fn dec_leaf(&mut self, pid: u64) -> Result<()> {
        if pid == 0 {
            return Ok(());
        }
        let v = self.get_rc(pid);
        if v > 1 {
            self.set_rc(pid, v - 1)?;
            return Ok(());
        }
        self.crash("before_free");
        let _ = fs::remove_file(self.rc_path(pid));
        fsync_dir(&self.root.join(RC_DIR))?;
        let _ = fs::remove_file(self.page_path(pid));
        fsync_dir(&self.root.join(DATA_DIR))?;
        self.crash("after_free");
        Ok(())
    }

    /// Remove a root page known to carry a single reference.
    fn drop_root_page(&mut self, root: u64) -> Result<()> {
        let v = self.get_rc(root);
        if v > 1 {
            // defensive: shared roots do not occur in the current design
            self.set_rc(root, v - 1)?;
            return Ok(());
        }
        self.crash("before_free");
        let _ = fs::remove_file(self.rc_path(root));
        fsync_dir(&self.root.join(RC_DIR))?;
        let _ = fs::remove_file(self.page_path(root));
        fsync_dir(&self.root.join(DATA_DIR))?;
        self.crash("after_free");
        Ok(())
    }

    /// Release a whole branch root: drop the root and decrement every leaf it named.
    fn release_root_tree(&mut self, root: u64, slots: &[u64]) -> Result<()> {
        let v = self.get_rc(root);
        if v > 1 {
            self.set_rc(root, v - 1)?;
            return Ok(());
        }
        self.crash("before_free");
        let _ = fs::remove_file(self.rc_path(root));
        fsync_dir(&self.root.join(RC_DIR))?;
        let _ = fs::remove_file(self.page_path(root));
        fsync_dir(&self.root.join(DATA_DIR))?;
        self.crash("after_free");
        for pid in slots.iter().copied() {
            if pid != 0 {
                self.dec_leaf(pid)?;
            }
        }
        Ok(())
    }

    // ---------- recovery ----------

    /// Rebuild truth from the committed MANIFEST:
    /// walk every root, recompute leaf/root refcounts, repair stale count files, and delete
    /// unreachable pages and leftover temp files. Idempotent; runs on every open.
    fn recover(&mut self) -> Result<RecoveryInfo> {
        let mut expected: BTreeMap<u64, u32> = BTreeMap::new();
        let mut max_id = self.next_id.saturating_sub(1);

        for (name, b) in &self.branches {
            if b.root == 0 {
                continue;
            }
            if !self.page_path(b.root).exists() {
                return Err(StoreError::Corrupt(format!(
                    "branch {name}: root page {} is missing",
                    b.root
                )));
            }
            *expected.entry(b.root).or_insert(0) += 1;
            max_id = max_id.max(b.root);
            for pid in self.branch_slots[name].iter().copied() {
                if pid == 0 {
                    continue;
                }
                if !self.page_path(pid).exists() {
                    return Err(StoreError::Corrupt(format!(
                        "branch {name} references missing page {pid}"
                    )));
                }
                *expected.entry(pid).or_insert(0) += 1;
                max_id = max_id.max(pid);
            }
        }

        // reconcile refcount files
        let rc_dir = self.root.join(RC_DIR);
        for ent in fs::read_dir(&rc_dir)? {
            let ent = ent?;
            let fname = ent.file_name();
            let Some(name) = fname.to_str() else {
                let _ = fs::remove_file(ent.path());
                continue;
            };
            let Some(stem) = name.strip_suffix(".rc") else {
                let _ = fs::remove_file(ent.path()); // leftover temp
                continue;
            };
            if let Ok(pid) = stem.parse::<u64>() {
                max_id = max_id.max(pid);
                match expected.get(&pid) {
                    Some(v) => {
                        let cur = fs::read_to_string(ent.path())
                            .ok()
                            .and_then(|s| s.trim().parse::<u32>().ok())
                            .unwrap_or(0);
                        if cur != *v {
                            self.set_rc(pid, *v)?;
                        }
                    }
                    None => {
                        let _ = fs::remove_file(ent.path());
                        fsync_dir(&rc_dir)?;
                    }
                }
            } else {
                let _ = fs::remove_file(ent.path());
            }
        }
        for (pid, v) in &expected {
            if !self.rc_path(*pid).exists() {
                self.set_rc(*pid, *v)?;
            }
        }

        // delete orphan pages and leftover temps
        let data_dir = self.root.join(DATA_DIR);
        let mut orphans = 0u64;
        for ent in fs::read_dir(&data_dir)? {
            let ent = ent?;
            let fname = ent.file_name();
            let Some(name) = fname.to_str() else { continue };
            let Some(stem) = name.strip_suffix(".page") else {
                if name.starts_with('.') {
                    let _ = fs::remove_file(ent.path());
                }
                continue;
            };
            match stem.parse::<u64>() {
                Ok(pid) => {
                    max_id = max_id.max(pid);
                    if !expected.contains_key(&pid) {
                        let _ = fs::remove_file(ent.path());
                        orphans += 1;
                    }
                }
                Err(_) => {
                    let _ = fs::remove_file(ent.path());
                }
            }
        }
        fsync_dir(&data_dir)?;

        // clean stale top-level temp files
        for ent in fs::read_dir(&self.root)? {
            let ent = ent?;
            if let Some(n) = ent.file_name().to_str() {
                if n.starts_with(&format!(".{MANIFEST}")) || n.starts_with(&format!(".{ALLOC}")) {
                    let _ = fs::remove_file(ent.path());
                }
            }
        }
        fsync_dir(&self.root)?;

        if self.next_id <= max_id {
            self.next_id = max_id + 1;
            self.persist_alloc()?;
        }
        self.persist_manifest()?;

        Ok(RecoveryInfo {
            orphans_removed: orphans,
            live_pages: expected.keys().copied().collect(),
        })
    }

    fn stats(&self) -> Result<Stats> {
        let mut refcounts = BTreeMap::new();
        let mut live = Vec::new();
        for (name, b) in &self.branches {
            if b.root != 0 {
                live.push(b.root);
            }
            for pid in self.branch_slots[name].iter().copied().filter(|p| *p != 0) {
                live.push(pid);
            }
        }
        live.sort_unstable();
        live.dedup();
        for pid in &live {
            refcounts.insert(*pid, self.get_rc(*pid));
        }
        Ok(Stats {
            page_size: PAGE_SIZE,
            slots: self.slots_n,
            next_id: self.next_id,
            pages_minted_total: self.next_id - 1,
            live_page_count: live.len(),
            live_pages: live,
            refcounts,
            branches: self.branch_infos(),
        })
    }
}

// ---------- free functions ----------

fn page_name(pid: u64) -> String {
    format!("{pid}.page")
}

fn encode_slots(slots: &[u64]) -> Vec<u8> {
    let mut out = Vec::with_capacity(slots.len() * 8);
    for p in slots {
        out.extend_from_slice(&p.to_le_bytes());
    }
    out
}

fn decode_slots(bytes: &[u8], n: usize) -> Result<Vec<u64>> {
    if bytes.len() != n * 8 {
        return Err(StoreError::Corrupt(format!(
            "root page has {} bytes, expected {}",
            bytes.len(),
            n * 8
        )));
    }
    Ok(bytes
        .chunks_exact(8)
        .map(|c| u64::from_le_bytes(c.try_into().unwrap()))
        .collect())
}

fn read_root_slots(path: &Path, n: usize) -> Result<Vec<u64>> {
    decode_slots(&fs::read(path)?, n)
}

fn fsync_dir(dir: &Path) -> std::io::Result<()> {
    fs::File::open(dir)?.sync_all()
}

fn validate_name(name: &str) -> Result<()> {
    if name.is_empty()
        || name.len() > 64
        || !name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '_' || c == '-')
    {
        return Err(StoreError::BadRequest(format!(
            "invalid branch name {name:?}"
        )));
    }
    Ok(())
}

fn now_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}
