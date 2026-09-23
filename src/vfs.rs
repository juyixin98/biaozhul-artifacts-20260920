//! Injectable I/O layer.
//!
//! All disk access performed by [`crate::store::Repository`] goes through the
//! [`Vfs`] trait, which makes two things possible:
//!
//! 1. **Deterministic failure injection** in tests: [`FaultFs`] wraps any other
//!    `Vfs` and injects I/O faults at chosen moments (e.g. failing the final
//!    rename of a root publish, simulating a crash *mid root switch*).
//! 2. **In-memory tests**: [`MemFs`] keeps the repository entirely in RAM so the
//!    concurrency test suite needs no temp directories.
//!
//! [`RealFs`] is the production backend. All multi-step writes use the same
//! pattern: write to a unique temp file in the target directory, fsync it, then
//! atomically rename into place (`create_new` semantics for block data so
//! concurrent identical uploads race safely).
//!
//! # Synchronization boundary
//!
//! The repository is designed to be owned by **one process at a time**:
//! `Repository::open` takes a non-blocking exclusive `flock(2)` on
//! `repo.lock`. Multiple threads inside that process are fully safe. A second
//! process opening the same directory gets [`StoreError::Locked`].

use std::collections::BTreeMap;
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::store::StoreError;

/// Filesystem operations the repository needs.
///
/// Paths are always absolute from the repository root's perspective; for
/// [`MemFs`] they are simply keys beginning with `/`.
pub trait Vfs: Send + Sync {
    /// Create a new file exclusively — fails with `AlreadyExists` if it exists.
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn Write + Send>>;
    /// Open an existing file for reading.
    fn open_read(&self, path: &Path) -> io::Result<Box<dyn Read + Send>>;
    /// Read a whole file into memory (blocks are fully buffered anyway).
    fn read(&self, path: &Path) -> io::Result<Vec<u8>> {
        let mut f = self.open_read(path)?;
        let mut buf = Vec::new();
        f.read_to_end(&mut buf)?;
        Ok(buf)
    }
    /// Rename atomically (same filesystem — temp files live beside targets).
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()>;
    /// Create a directory (and parents). No error if it already exists.
    fn mkdir_p(&self, path: &Path) -> io::Result<()>;
    /// Does the path exist (file or directory)?
    fn exists(&self, path: &Path) -> bool;
    /// Direct children of a directory. Only regular files and directories are
    /// reported; the kind distinguishes them.
    fn list_dir(&self, path: &Path) -> io::Result<Vec<(String, EntryKind)>>;
    /// Remove a file.
    fn remove_file(&self, path: &Path) -> io::Result<()>;
    /// fsync a file (no-op for backends without durability semantics).
    fn sync_file(&self, path: &Path) -> io::Result<()>;
    /// fsync a directory after a rename/unlink so the metadata change is
    /// durable. Real implementation; no-op elsewhere.
    fn sync_dir(&self, path: &Path) -> io::Result<()>;
    /// Whether this backend persists across `Repository` opens.
    fn persistent(&self) -> bool;
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EntryKind {
    File,
    Dir,
}

// ---------------------------------------------------------------------------
// RealFs
// ---------------------------------------------------------------------------

/// Production backend backed by the operating system.
pub struct RealFs;

impl RealFs {
    pub fn new() -> Self {
        RealFs
    }
}

impl Default for RealFs {
    fn default() -> Self {
        Self::new()
    }
}

impl Vfs for RealFs {
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn Write + Send>> {
        let f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)?;
        Ok(Box::new(f))
    }

    fn open_read(&self, path: &Path) -> io::Result<Box<dyn Read + Send>> {
        Ok(Box::new(std::fs::File::open(path)?))
    }

    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        std::fs::rename(from, to)
    }

    fn mkdir_p(&self, path: &Path) -> io::Result<()> {
        std::fs::create_dir_all(path)
    }

    fn exists(&self, path: &Path) -> bool {
        path.symlink_metadata().is_ok()
    }

    fn list_dir(&self, path: &Path) -> io::Result<Vec<(String, EntryKind)>> {
        let mut out = Vec::new();
        for ent in std::fs::read_dir(path)? {
            let ent = ent?;
            let kind = if ent.file_type()?.is_dir() {
                EntryKind::Dir
            } else {
                EntryKind::File
            };
            if let Some(name) = ent.file_name().to_str() {
                out.push((name.to_string(), kind));
            }
        }
        Ok(out)
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        std::fs::remove_file(path)
    }

    fn sync_file(&self, path: &Path) -> io::Result<()> {
        std::fs::File::open(path)?.sync_all()
    }

    fn sync_dir(&self, path: &Path) -> io::Result<()> {
        std::fs::File::open(path)?.sync_all()
    }

    fn persistent(&self) -> bool {
        true
    }
}

// ---------------------------------------------------------------------------
// Atomic write helper (shared by all backends)
// ---------------------------------------------------------------------------

/// Write `data` to `target` atomically: unique temp file in `dir`, fsync,
/// rename, fsync directory.
///
/// If the rename fails (including an injected fault), the temp file is left on
/// disk — exactly what a crash at that instant would produce. `open` sweeps
/// stale `.tmp.` files so they never accumulate.
pub(crate) fn atomic_write(
    vfs: &dyn Vfs,
    dir: &Path,
    target: &Path,
    data: &[u8],
) -> Result<(), StoreError> {
    let tmp = dir.join(format!(
        ".tmp.{}.{}.{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0),
        rnd_suffix()
    ));
    {
        let mut f = vfs
            .create_new(&tmp)
            .map_err(|e| StoreError::io("create temp", e))?;
        f.write_all(data).map_err(|e| StoreError::io("write", e))?;
        f.flush().map_err(|e| StoreError::io("flush", e))?;
        drop(f);
        vfs.sync_file(&tmp).ok(); // durability best-effort
    }
    vfs.rename(&tmp, target)
        .map_err(|e| StoreError::io("rename publish", e))?;
    vfs.sync_dir(dir).ok();
    Ok(())
}

fn rnd_suffix() -> u64 {
    use std::sync::atomic::{AtomicU64, Ordering};
    static CTR: AtomicU64 = AtomicU64::new(0x9e37_79b9_7f4a_7c15);
    let n = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0);
    n.wrapping_mul(6364136223846793005)
        .wrapping_add(CTR.fetch_add(1, Ordering::Relaxed))
}

// ---------------------------------------------------------------------------
// MemFs
// ---------------------------------------------------------------------------

#[derive(Clone)]
enum MemNode {
    Dir,
    File(Arc<Vec<u8>>),
}

/// Fully in-memory filesystem for fast, deterministic tests.
///
/// The tree is a flat `BTreeMap` from normalized absolute path to node;
/// directory listings are derived from key prefixes, so `rename` is a single
/// remove + insert and can never disagree with some separate child index.
#[derive(Clone)]
pub struct MemFs {
    nodes: Arc<Mutex<BTreeMap<String, MemNode>>>,
}

impl MemFs {
    pub fn new() -> Self {
        let mut nodes = BTreeMap::new();
        nodes.insert("/".to_string(), MemNode::Dir);
        MemFs {
            nodes: Arc::new(Mutex::new(nodes)),
        }
    }

    fn norm(p: &Path) -> String {
        let s = p.to_string_lossy().into_owned();
        if s == "/" {
            "/".to_string()
        } else {
            s.trim_end_matches('/').to_string()
        }
    }
}

impl Default for MemFs {
    fn default() -> Self {
        Self::new()
    }
}

/// Publishes buffered bytes on drop unless the key was taken in the meantime
/// (e.g. a successful rename removed the temp key before another drop) or an
/// error was recorded (`abort`): failed writes leave nothing observable.
struct MemWriter {
    fs: MemFs,
    path: String,
    buf: Vec<u8>,
    /// Set when a write fails: drop then REMOVES any partial node instead of
    /// publishing it, matching a real filesystem abandoning a temp file.
    aborted: bool,
}
impl Write for MemWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.buf.extend_from_slice(buf);
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}
impl Drop for MemWriter {
    fn drop(&mut self) {
        let mut nodes = self.fs.nodes.lock().unwrap();
        if self.aborted {
            nodes.remove(&self.path);
            return;
        }
        if !nodes.contains_key(&self.path) {
            nodes.insert(
                self.path.clone(),
                MemNode::File(Arc::new(std::mem::take(&mut self.buf))),
            );
        }
    }
}

struct MemReader(Arc<Vec<u8>>, usize);
impl Read for MemReader {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let data = &self.0[self.1..];
        let n = data.len().min(buf.len());
        buf[..n].copy_from_slice(&data[..n]);
        self.1 += n;
        Ok(n)
    }
}

impl Vfs for MemFs {
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn Write + Send>> {
        let key = Self::norm(path);
        {
            let nodes = self.nodes.lock().unwrap();
            if nodes.contains_key(&key) {
                return Err(io::Error::new(io::ErrorKind::AlreadyExists, key));
            }
        }
        Ok(Box::new(MemWriter {
            fs: self.clone(),
            path: key,
            buf: Vec::new(),
            aborted: false,
        }))
    }

    fn open_read(&self, path: &Path) -> io::Result<Box<dyn Read + Send>> {
        let key = Self::norm(path);
        let nodes = self.nodes.lock().unwrap();
        match nodes.get(&key) {
            Some(MemNode::File(data)) => Ok(Box::new(MemReader(data.clone(), 0))),
            Some(MemNode::Dir) => {
                Err(io::Error::new(io::ErrorKind::PermissionDenied, "is a directory"))
            }
            None => Err(io::Error::new(io::ErrorKind::NotFound, key)),
        }
    }

    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        let src = Self::norm(from);
        let dst = Self::norm(to);
        let mut nodes = self.nodes.lock().unwrap();
        let node = nodes
            .remove(&src)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, src.clone()))?;
        // Mirror rename(2): replacing an existing file is allowed, replacing a
        // non-empty directory is not (repository never renames directories).
        if let Some(MemNode::Dir) = nodes.get(&dst) {
            let prefix = format!("{}/", dst);
            let has_child = nodes
                .range(prefix.clone()..)
                .next()
                .map(|(k, _)| k.starts_with(&prefix))
                .unwrap_or(false);
            nodes.insert(src, node);
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                if has_child {
                    "target dir not empty"
                } else {
                    "target is a directory"
                },
            ));
        }
        nodes.insert(dst, node);
        Ok(())
    }

    fn mkdir_p(&self, path: &Path) -> io::Result<()> {
        let key = Self::norm(path);
        let mut nodes = self.nodes.lock().unwrap();
        let parts: Vec<&str> = key.split('/').filter(|s| !s.is_empty()).collect();
        let mut cur = "/".to_string();
        for part in parts {
            let next = if cur == "/" {
                format!("/{}", part)
            } else {
                format!("{}/{}", cur, part)
            };
            match nodes.get(&next) {
                Some(MemNode::Dir) => {}
                Some(MemNode::File(_)) => {
                    return Err(io::Error::new(io::ErrorKind::AlreadyExists, next))
                }
                None => {
                    nodes.insert(next.clone(), MemNode::Dir);
                }
            }
            cur = next;
        }
        Ok(())
    }

    fn exists(&self, path: &Path) -> bool {
        self.nodes.lock().unwrap().contains_key(&Self::norm(path))
    }

    fn list_dir(&self, path: &Path) -> io::Result<Vec<(String, EntryKind)>> {
        let key = Self::norm(path);
        let nodes = self.nodes.lock().unwrap();
        if !matches!(nodes.get(&key), Some(MemNode::Dir)) {
            return Err(io::Error::new(io::ErrorKind::NotFound, key));
        }
        let prefix = if key == "/" {
            "/".to_string()
        } else {
            format!("{}/", key)
        };
        let mut out = Vec::new();
        for (k, node) in nodes.range(prefix.clone()..) {
            if !k.starts_with(&prefix) {
                break;
            }
            let rest = &k[prefix.len()..];
            if rest.is_empty() || rest.contains('/') {
                continue;
            }
            let kind = match node {
                MemNode::Dir => EntryKind::Dir,
                MemNode::File(_) => EntryKind::File,
            };
            out.push((rest.to_string(), kind));
        }
        Ok(out)
    }

    fn remove_file(&self, path: &Path) -> io::Result<()> {
        let key = Self::norm(path);
        let mut nodes = self.nodes.lock().unwrap();
        match nodes.remove(&key) {
            Some(MemNode::File(_)) => Ok(()),
            Some(d @ MemNode::Dir) => {
                nodes.insert(key, d);
                Err(io::Error::new(io::ErrorKind::PermissionDenied, "is a directory"))
            }
            None => Err(io::Error::new(io::ErrorKind::NotFound, key)),
        }
    }

    fn sync_file(&self, _path: &Path) -> io::Result<()> {
        Ok(())
    }
    fn sync_dir(&self, _path: &Path) -> io::Result<()> {
        Ok(())
    }
    fn persistent(&self) -> bool {
        false
    }
}

// ---------------------------------------------------------------------------
// FaultFs
// ---------------------------------------------------------------------------

/// Kind of operation that can be sabotaged.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultOp {
    /// Failure of the atomic `rename` (simulates crash mid-publish).
    Rename,
    /// Failure while writing file bytes.
    Write,
    /// Failure of `create_new` (e.g. temp file allocation problem).
    Create,
}

/// One injected failure rule.
#[derive(Debug, Clone)]
pub struct Fault {
    /// Fires when the destination path contains this substring.
    pub path_contains: &'static str,
    pub op: FaultOp,
    /// Fire on the 1-based nth matching call, then disarm (None = every time).
    pub once: Option<u64>,
}

/// A `Vfs` decorator that injects faults.
///
/// Rename faults leave the temp file in place: this models a crash between
/// "data written" and "rename published" rather than a clean error.
pub struct FaultFs {
    inner: Arc<dyn Vfs>,
    faults: Mutex<Vec<(Fault, u64)>>,
}

impl FaultFs {
    pub fn new(inner: Arc<dyn Vfs>, faults: Vec<Fault>) -> Self {
        FaultFs {
            inner,
            faults: Mutex::new(faults.into_iter().map(|f| (f, 0)).collect()),
        }
    }

    fn fires(&self, path: &Path, op: FaultOp) -> bool {
        let p = path.to_string_lossy();
        let mut faults = self.faults.lock().unwrap();
        for (rule, seen) in faults.iter_mut() {
            if rule.op == op && p.contains(rule.path_contains) {
                *seen += 1;
                match rule.once {
                    Some(n) if *seen == n => return true,
                    _ => continue,
                }
            }
        }
        false
    }
}

impl Vfs for FaultFs {
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn Write + Send>> {
        if self.fires(path, FaultOp::Create) {
            return Err(io::Error::new(io::ErrorKind::Other, "injected create fault"));
        }
        let writer = self.inner.create_new(path)?;
        Ok(Box::new(FaultWriter {
            path: path.to_path_buf(),
            fs: FaultFsPtr(self as *const FaultFs),
            inner: Some(writer),
            failed: false,
        }))
    }

    fn open_read(&self, path: &Path) -> io::Result<Box<dyn Read + Send>> {
        self.inner.open_read(path)
    }

    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        // Match against the destination: all our atomic-write rules name the
        // final path. The temp file is deliberately NOT removed on failure.
        if self.fires(to, FaultOp::Rename) {
            return Err(io::Error::new(io::ErrorKind::Other, "injected rename fault"));
        }
        self.inner.rename(from, to)
    }

    fn mkdir_p(&self, path: &Path) -> io::Result<()> {
        self.inner.mkdir_p(path)
    }
    fn exists(&self, path: &Path) -> bool {
        self.inner.exists(path)
    }
    fn list_dir(&self, path: &Path) -> io::Result<Vec<(String, EntryKind)>> {
        self.inner.list_dir(path)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        self.inner.remove_file(path)
    }
    fn sync_file(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_file(path)
    }
    fn sync_dir(&self, path: &Path) -> io::Result<()> {
        self.inner.sync_dir(path)
    }
    fn persistent(&self) -> bool {
        self.inner.persistent()
    }
}

/// Pointer to the parent [`FaultFs`]. Raw pointers are not `Send`, but the
/// pointed-to FaultFs (owned by a Repository) outlives every writer and is
/// itself `Sync`, so forwarding the pointer across threads is sound here.
struct FaultFsPtr(*const FaultFs);
unsafe impl Send for FaultFsPtr {}

struct FaultWriter {
    path: PathBuf,
    fs: FaultFsPtr,
    inner: Option<Box<dyn Write + Send>>,
    /// Set when an injected write fault fired: drop must remove the partial
    /// temp file instead of letting it linger.
    failed: bool,
}

impl FaultWriter {
    fn fault_fs(&self) -> &FaultFs {
        unsafe { &*self.fs.0 }
    }
}

impl Write for FaultWriter {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if self.fault_fs().fires(&self.path, FaultOp::Write) {
            self.failed = true;
            return Err(io::Error::new(io::ErrorKind::Other, "injected write fault"));
        }
        self.inner.as_mut().unwrap().write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.inner.as_mut().unwrap().flush()
    }
}

impl Drop for FaultWriter {
    fn drop(&mut self) {
        // Explicitly close the inner writer first (MemFs publishes on drop),
        // then, if this write failed, remove the partial temp file so the
        // failed upload has zero observable effect (matching a real disk error
        // followed by temp-file cleanup).
        drop(self.inner.take());
        if self.failed {
            let _ = self.fault_fs().inner.remove_file(&self.path);
        }
    }
}

// ---------------------------------------------------------------------------
// Advisory inter-process lock
// ---------------------------------------------------------------------------

/// Non-blocking exclusive advisory lock on a file inside the repo.
///
/// Held for the lifetime of a [`crate::store::Repository`]; released on drop.
pub struct FileLock {
    file: std::fs::File,
}

impl FileLock {
    /// Acquire `LOCK_EX | LOCK_NB` on `path` (creating the file if needed).
    pub fn try_exclusive(path: &Path) -> Result<Self, StoreError> {
        let file = std::fs::OpenOptions::new()
            .create(true)
            .write(true)
            .open(path)
            .map_err(|e| StoreError::io("open lock", e))?;
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
            if rc != 0 {
                let err = io::Error::last_os_error();
                if err.raw_os_error() == Some(libc::EWOULDBLOCK) {
                    return Err(StoreError::Locked);
                }
                return Err(StoreError::io("flock", err));
            }
        }
        // On non-Unix targets only in-process synchronization is provided;
        // the deployment targets of this project are Unix.
        #[cfg(not(unix))]
        let _ = &file;
        Ok(FileLock { file })
    }
}

impl Drop for FileLock {
    fn drop(&mut self) {
        #[cfg(unix)]
        {
            use std::os::fd::AsRawFd;
            unsafe {
                libc::flock(self.file.as_raw_fd(), libc::LOCK_UN);
            }
        }
    }
}
