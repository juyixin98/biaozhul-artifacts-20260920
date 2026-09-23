//! Pluggable I/O layer.
//!
//! Every disk operation the engine performs goes through [`Vfs`]. Two
//! implementations exist:
//!
//! * [`RealVfs`] — the operating-system file system with explicit
//!   `File::sync_all()` durability points.
//! * [`MemVfs`] — an in-memory file system used by tests, including the
//!   "simulate a crash and reopen" scenarios.
//!
//! [`FaultVfs`] wraps either implementation and deterministically fails the
//! next matching operation (optionally leaving an fsynced/unlinked file in
//! place), which is how crash/injection tests observe the sync boundaries.

use std::collections::BTreeMap;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use crate::error::Error;

/// Directory entry returned by [`Vfs::list_dir`].
#[derive(Debug, Clone)]
pub struct DirEntry {
    pub name: String,
    pub is_file: bool,
}

/// A seekable, readable, writable file handle.
pub trait VFile: Read + Write + Seek + Send {
    fn sync_all(&mut self) -> io::Result<()> {
        Ok(())
    }
}

/// Minimal file-system surface the engine depends on.
///
/// All methods take `&self` so implementations can be shared cheaply
/// (`Arc<dyn Vfs>`). Implementations are responsible for their own
/// synchronization.
pub trait Vfs: Send + Sync {
    fn create_dir_all(&self, path: &Path) -> io::Result<()>;
    fn exists(&self, path: &Path) -> bool;
    fn is_dir(&self, path: &Path) -> bool;
    fn list_dir(&self, path: &Path) -> io::Result<Vec<DirEntry>>;
    fn open_read(&self, path: &Path) -> io::Result<Box<dyn VFile>>;
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn VFile>>;
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()>;
    fn remove_file(&self, path: &Path) -> io::Result<()>;

    /// Atomically create/replace `dst` with `bytes`:
    /// write temp file → fsync temp → rename → fsync parent directory.
    ///
    /// This is the unit of durable publication for both transaction commit
    /// segments and compacted base files.
    fn write_atomic(&self, dst: &Path, bytes: &[u8]) -> crate::error::Result<()> {
        let tmp = sibling_tmp(dst);
        {
            let mut f = self.create_new(&tmp)?;
            f.write_all(bytes)?;
            f.sync_all()?;
        }
        self.rename(&tmp, dst)?;
        self.sync_parent(dst)?;
        Ok(())
    }

    /// fsync the containing directory (required to make a rename durable).
    fn sync_parent(&self, _path: &Path) -> io::Result<()> {
        Ok(())
    }
}

pub type SharedVfs = Arc<dyn Vfs>;

fn sibling_tmp(dst: &Path) -> PathBuf {
    let name = dst
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_else(|| "file".into());
    dst.with_file_name(format!(".{name}.tmp"))
}

// ---------------------------------------------------------------------------
// Real file system
// ---------------------------------------------------------------------------

pub struct RealFile(std::fs::File);

impl VFile for RealFile {
    fn sync_all(&mut self) -> io::Result<()> {
        self.0.sync_all()
    }
}

impl Read for RealFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.0.read(buf)
    }
}
impl Write for RealFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0.flush()
    }
}
impl Seek for RealFile {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        self.0.seek(pos)
    }
}

#[derive(Default)]
pub struct RealVfs;

impl RealVfs {
    pub fn new() -> Self {
        RealVfs
    }
}

impl Vfs for RealVfs {
    fn create_dir_all(&self, path: &Path) -> io::Result<()> {
        std::fs::create_dir_all(path)
    }
    fn exists(&self, path: &Path) -> bool {
        path.exists()
    }
    fn is_dir(&self, path: &Path) -> bool {
        path.is_dir()
    }
    fn list_dir(&self, path: &Path) -> io::Result<Vec<DirEntry>> {
        let mut out = Vec::new();
        for e in std::fs::read_dir(path)? {
            let e = e?;
            let ft = e.file_type()?;
            out.push(DirEntry {
                name: e.file_name().to_string_lossy().into_owned(),
                is_file: ft.is_file(),
            });
        }
        Ok(out)
    }
    fn open_read(&self, path: &Path) -> io::Result<Box<dyn VFile>> {
        Ok(Box::new(RealFile(std::fs::File::open(path)?)))
    }
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn VFile>> {
        Ok(Box::new(RealFile(std::fs::File::create(path)?)))
    }
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        std::fs::rename(from, to)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        std::fs::remove_file(path)
    }
    fn sync_parent(&self, path: &Path) -> io::Result<()> {
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        let f = std::fs::File::open(parent)?;
        f.sync_all()
    }
}

// ---------------------------------------------------------------------------
// In-memory file system (tests)
// ---------------------------------------------------------------------------

type MemTree = BTreeMap<PathBuf, Vec<u8>>;

#[derive(Default)]
struct MemState {
    files: MemTree,
    dirs: BTreeMap<PathBuf, ()>,
}

/// In-memory file system. Cloning a `MemVfs` shares the same backing store
/// (Arc inside), which is how tests "reopen" a data directory.
#[derive(Clone, Default)]
pub struct MemVfs {
    state: Arc<Mutex<MemState>>,
}

impl MemVfs {
    pub fn new() -> Self {
        let vfs = MemVfs::default();
        vfs.create_dir_all(Path::new("/")).ok();
        vfs
    }

    /// Number and total bytes of regular files currently stored (any path).
    pub fn file_count_and_bytes(&self) -> (usize, u64) {
        let s = self.state.lock().unwrap();
        let bytes = s.files.values().map(|b| b.len() as u64).sum();
        (s.files.len(), bytes)
    }

    /// Total bytes stored under `prefix` (used by GC tests).
    pub fn bytes_under(&self, prefix: &Path) -> (usize, u64) {
        let s = self.state.lock().unwrap();
        let mut n = 0;
        let mut bytes = 0u64;
        for (p, b) in &s.files {
            if p.starts_with(prefix) {
                n += 1;
                bytes += b.len() as u64;
            }
        }
        (n, bytes)
    }

    pub fn list_paths(&self) -> Vec<PathBuf> {
        self.state.lock().unwrap().files.keys().cloned().collect()
    }

    fn with_state<R>(&self, f: impl FnOnce(&mut MemState) -> R) -> R {
        let mut s = self.state.lock().unwrap();
        f(&mut s)
    }
}

struct MemFile {
    state: Arc<Mutex<MemState>>,
    path: PathBuf,
    pos: u64,
}

impl MemFile {
    fn buf_len(&self) -> u64 {
        self.state
            .lock()
            .unwrap()
            .files
            .get(&self.path)
            .map(|b| b.len() as u64)
            .unwrap_or(0)
    }
}

impl Read for MemFile {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        let s = self.state.lock().unwrap();
        let data = s
            .files
            .get(&self.path)
            .ok_or_else(|| io::Error::new(io::ErrorKind::NotFound, "mem file missing"))?;
        let pos = self.pos as usize;
        if pos >= data.len() {
            return Ok(0);
        }
        let n = buf.len().min(data.len() - pos);
        buf[..n].copy_from_slice(&data[pos..pos + n]);
        self.pos += n as u64;
        Ok(n)
    }
}

impl Write for MemFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        let mut s = self.state.lock().unwrap();
        let data = s.files.entry(self.path.clone()).or_default();
        let pos = self.pos as usize;
        if pos > data.len() {
            data.resize(pos, 0);
        }
        let end = pos + buf.len();
        if end > data.len() {
            data.resize(end, 0);
        }
        data[pos..end].copy_from_slice(buf);
        self.pos = end as u64;
        Ok(buf.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

impl Seek for MemFile {
    fn seek(&mut self, pos: SeekFrom) -> io::Result<u64> {
        let len = self.buf_len() as i64;
        let new = match pos {
            SeekFrom::Start(n) => n as i64,
            SeekFrom::Current(n) => self.pos as i64 + n,
            SeekFrom::End(n) => len + n,
        };
        if new < 0 {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "negative seek"));
        }
        self.pos = new as u64;
        Ok(self.pos)
    }
}

impl VFile for MemFile {}

impl Vfs for MemVfs {
    fn create_dir_all(&self, path: &Path) -> io::Result<()> {
        self.with_state(|s| {
            let mut p = PathBuf::new();
            for c in path.components() {
                p.push(c.as_os_str());
                s.dirs.insert(p.clone(), ());
            }
        });
        Ok(())
    }
    fn exists(&self, path: &Path) -> bool {
        self.with_state(|s| s.files.contains_key(path) || s.dirs.contains_key(path))
    }
    fn is_dir(&self, path: &Path) -> bool {
        self.with_state(|s| s.dirs.contains_key(path) && !s.files.contains_key(path))
    }
    fn list_dir(&self, path: &Path) -> io::Result<Vec<DirEntry>> {
        self.with_state(|s| {
            let mut out = Vec::new();
            let mut names: BTreeMap<String, bool> = BTreeMap::new();
            for f in s.files.keys() {
                if let Some(name) = direct_child(path, f) {
                    names.insert(name, true);
                }
            }
            for d in s.dirs.keys() {
                if let Some(name) = direct_child(path, d) {
                    names.entry(name).or_insert(false);
                }
            }
            for (name, is_file) in names {
                out.push(DirEntry { name, is_file });
            }
            Ok(out)
        })
    }
    fn open_read(&self, path: &Path) -> io::Result<Box<dyn VFile>> {
        let exists = self.with_state(|s| s.files.contains_key(path));
        if !exists {
            return Err(io::Error::new(
                io::ErrorKind::NotFound,
                path.to_string_lossy().as_ref(),
            ));
        }
        Ok(Box::new(MemFile {
            state: self.state.clone(),
            path: path.to_path_buf(),
            pos: 0,
        }))
    }
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn VFile>> {
        self.with_state(|s| {
            s.files.insert(path.to_path_buf(), Vec::new());
            if let Some(parent) = path.parent() {
                let mut p = PathBuf::new();
                for c in parent.components() {
                    p.push(c.as_os_str());
                    s.dirs.insert(p.clone(), ());
                }
            }
        });
        Ok(Box::new(MemFile {
            state: self.state.clone(),
            path: path.to_path_buf(),
            pos: 0,
        }))
    }
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        self.with_state(|s| match s.files.remove(from) {
            Some(data) => {
                s.files.insert(to.to_path_buf(), data);
                Ok(())
            }
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                from.to_string_lossy().as_ref(),
            )),
        })
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        self.with_state(|s| match s.files.remove(path) {
            Some(_) => Ok(()),
            None => Err(io::Error::new(
                io::ErrorKind::NotFound,
                path.to_string_lossy().as_ref(),
            )),
        })
    }
}

fn direct_child(dir: &Path, child: &Path) -> Option<String> {
    child.strip_prefix(dir).ok().and_then(|rel| {
        let mut comps = rel.components();
        let first = comps.next()?;
        comps
            .next()
            .is_none()
            .then(|| first.as_os_str().to_string_lossy().into_owned())
    })
}

// ---------------------------------------------------------------------------
// Fault injection wrapper
// ---------------------------------------------------------------------------

/// Kinds of I/O operations that can be forced to fail.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultOp {
    /// Data write into a new segment/base file.
    Write,
    /// `fsync` of a new segment/base file (the commit durability barrier).
    Sync,
    /// Rename of temp file to its published name.
    Rename,
    /// fsync of the parent directory after rename.
    SyncParent,
    /// Removal of an obsolete segment/base file during GC.
    Remove,
}

impl FaultOp {
    pub fn as_str(self) -> &'static str {
        match self {
            FaultOp::Write => "write",
            FaultOp::Sync => "sync",
            FaultOp::Rename => "rename",
            FaultOp::SyncParent => "sync_parent",
            FaultOp::Remove => "remove",
        }
    }
    pub fn parse(s: &str) -> Option<FaultOp> {
        Some(match s {
            "write" => FaultOp::Write,
            "sync" => FaultOp::Sync,
            "rename" => FaultOp::Rename,
            "sync_parent" => FaultOp::SyncParent,
            "remove" => FaultOp::Remove,
            _ => return None,
        })
    }
}

/// A one-shot fault rule: the next `op` operation fails; if `leave_written`
/// is true the data/rename is still applied before the error is returned,
/// simulating a crash *after* the bytes are on disk but *before* the caller
/// learns the operation completed.
#[derive(Clone)]
struct FaultRule {
    op: FaultOp,
    leave_written: bool,
}

/// Wraps any [`Vfs`] and deterministically injects the next fault.
#[derive(Clone)]
pub struct FaultVfs {
    inner: SharedVfs,
    rule: Arc<Mutex<Option<FaultRule>>>,
}

impl FaultVfs {
    pub fn new(inner: SharedVfs) -> Self {
        FaultVfs {
            inner,
            rule: Arc::new(Mutex::new(None)),
        }
    }

    /// Arm a fault: the next matching `op` returns an [`Error::Injected`]
    /// style I/O error.
    pub fn arm(&self, op: FaultOp, leave_written: bool) {
        *self.rule.lock().unwrap() = Some(FaultRule { op, leave_written });
    }

    /// Disarm and return the pending rule, if any.
    pub fn disarm(&self) -> Option<(FaultOp, bool)> {
        self.rule
            .lock()
            .unwrap()
            .take()
            .map(|r| (r.op, r.leave_written))
    }

    pub fn armed(&self) -> Option<FaultOp> {
        self.rule.lock().unwrap().as_ref().map(|r| r.op)
    }

    fn take_if(&self, op: FaultOp) -> Option<bool> {
        let mut g = self.rule.lock().unwrap();
        if let Some(rule) = g.as_ref() {
            if rule.op == op {
                let leave = rule.leave_written;
                *g = None;
                return Some(leave);
            }
        }
        None
    }

    fn injected_err(op: FaultOp) -> io::Error {
        io::Error::other(Error::Injected(format!(
            "{} failed (injected)",
            op.as_str()
        )))
    }
}

impl Vfs for FaultVfs {
    fn create_dir_all(&self, path: &Path) -> io::Result<()> {
        self.inner.create_dir_all(path)
    }
    fn exists(&self, path: &Path) -> bool {
        self.inner.exists(path)
    }
    fn is_dir(&self, path: &Path) -> bool {
        self.inner.is_dir(path)
    }
    fn list_dir(&self, path: &Path) -> io::Result<Vec<DirEntry>> {
        self.inner.list_dir(path)
    }
    fn open_read(&self, path: &Path) -> io::Result<Box<dyn VFile>> {
        self.inner.open_read(path)
    }
    fn create_new(&self, path: &Path) -> io::Result<Box<dyn VFile>> {
        self.inner.create_new(path)
    }
    fn rename(&self, from: &Path, to: &Path) -> io::Result<()> {
        if let Some(leave) = self.take_if(FaultOp::Rename) {
            if leave {
                self.inner.rename(from, to)?;
            }
            return Err(Self::injected_err(FaultOp::Rename));
        }
        self.inner.rename(from, to)
    }
    fn remove_file(&self, path: &Path) -> io::Result<()> {
        if self.take_if(FaultOp::Remove).is_some() {
            return Err(Self::injected_err(FaultOp::Remove));
        }
        self.inner.remove_file(path)
    }
    fn sync_parent(&self, path: &Path) -> io::Result<()> {
        if self.take_if(FaultOp::SyncParent).is_some() {
            return Err(Self::injected_err(FaultOp::SyncParent));
        }
        self.inner.sync_parent(path)
    }
    fn write_atomic(&self, dst: &Path, bytes: &[u8]) -> crate::error::Result<()> {
        let tmp = sibling_tmp(dst);
        {
            let mut f = self.create_new(&tmp)?;
            // Inject write fault mid-stream: write a prefix first so a torn
            // temp file is observable on disk, then fail.
            if let Some(leave) = self.take_if(FaultOp::Write) {
                if leave {
                    let half = bytes.len() / 2;
                    f.write_all(&bytes[..half])?;
                    f.sync_all()?;
                }
                return Err(Error::Injected("write failed (injected)".into()));
            }
            f.write_all(bytes)?;
            if let Some(leave) = self.take_if(FaultOp::Sync) {
                if leave {
                    f.sync_all()?;
                }
                return Err(Error::Injected("sync failed (injected)".into()));
            }
            f.sync_all()?;
        }
        if let Some(leave) = self.take_if(FaultOp::Rename) {
            if leave {
                self.rename(&tmp, dst)?;
            }
            return Err(Error::Injected("rename failed (injected)".into()));
        }
        self.rename(&tmp, dst)?;
        self.sync_parent(dst)?;
        Ok(())
    }
}
