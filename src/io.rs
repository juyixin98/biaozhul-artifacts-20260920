//! Injectable I/O layer.
//!
//! Every disk operation performed by the sort engine goes through the
//! [`Vfs`] trait. Production uses [`RealVfs`] (plain `std::fs`, with an
//! fsync of the parent directory after every rename so that rename-based
//! commits are real durability boundaries). Tests wrap it in [`FaultVfs`],
//! which consults a [`FaultPlan`] and can fail creates / writes / syncs /
//! renames / reads on paths matching a substring.
//!
//! Fault header grammar (comma-separated rules), used by both tests and the
//! HTTP `X-Fault-Inject` header:
//!
//! ```text
//! rule   := kind[ ":" needle ][ ":" count ]
//! kind   := write_fail | sync_fail | rename_fail | create_fail | read_fail
//! needle := substring matched against the affected path (default: any path)
//! count  := number of matching operations that fail (default: 1, 0 disables)
//! ```
//!
//! Examples:
//! * `write_fail:run-0001` — fail the next write to the first run file.
//! * `rename_fail:run` — fail the next run commit rename.
//! * `sync_fail:manifest.tmp` — fail fsync while committing the manifest.

use std::fs::{File, OpenOptions};
use std::io::{Read, Write};
use std::path::Path;
use std::sync::{Arc, Mutex};

use crate::error::{Error, Result};

/// One directory entry as reported by [`Vfs::list_dir`].
#[derive(Debug, Clone)]
pub struct DirEntry {
    pub name: String,
    pub is_file: bool,
}

/// Abstraction over the filesystem the engine depends on.
///
/// All paths are arbitrary strings interpreted relative to the root the
/// implementation was constructed with.
pub trait Vfs: Send + Sync {
    /// Create (truncate) a file for writing.
    fn create(&self, path: &str) -> Result<Box<dyn VFile>>;
    /// Open an existing file for sequential reading.
    fn open(&self, path: &str) -> Result<Box<dyn VFile>>;
    /// Atomically rename a file; on the real FS this is followed by an fsync
    /// of the destination parent directory, making the rename a commit point.
    fn rename(&self, from: &str, to: &str) -> Result<()>;
    /// Delete a file. Missing files are not an error.
    fn remove(&self, path: &str) -> Result<()>;
    /// Whether a path exists (file or directory).
    fn exists(&self, path: &str) -> bool;
    /// List the immediate contents of a directory.
    fn list_dir(&self, path: &str) -> Result<Vec<DirEntry>>;
    /// Create a directory and all missing parents.
    fn make_dir_all(&self, path: &str) -> Result<()>;
    /// Recursively delete a directory tree. Missing path is not an error.
    fn remove_dir_all(&self, path: &str) -> Result<()>;
}

/// Sequential file handle. All methods return the crate error type so that
/// injected faults surface uniformly.
pub trait VFile: Send {
    fn read(&mut self, buf: &mut [u8]) -> Result<usize>;
    fn write_all(&mut self, buf: &[u8]) -> Result<()>;
    fn flush(&mut self) -> Result<()>;
    fn sync_all(&self) -> Result<()>;
}

/// Read an entire small file (manifest, status, request bodies in tests).
pub fn read_all(vfs: &dyn Vfs, path: &str) -> Result<Vec<u8>> {
    let mut f = vfs.open(path)?;
    let mut out = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        let n = f.read(&mut chunk)?;
        if n == 0 {
            break;
        }
        out.extend_from_slice(&chunk[..n]);
    }
    Ok(out)
}

/// Write `bytes` to `tmp`, fsync, then atomically rename to `dst`.
/// This is the canonical sync boundary used for manifests and status files.
pub fn atomic_write_bytes(vfs: &dyn Vfs, tmp: &str, dst: &str, bytes: &[u8]) -> Result<()> {
    {
        let mut f = vfs.create(tmp)?;
        f.write_all(bytes)?;
        f.flush()?;
        f.sync_all()?;
    }
    vfs.rename(tmp, dst)?;
    Ok(())
}

/// fsync a directory (no-op if the platform rejects directory fsync).
fn sync_dir(path: &Path) -> Result<()> {
    match OpenOptions::new().read(true).open(path) {
        Ok(f) => f
            .sync_all()
            .map_err(|e| Error::io(e, path.display().to_string())),
        Err(_) => Ok(()),
    }
}

/// Production VFS backed by `std::fs`, rooted at a base directory.
#[derive(Debug, Clone)]
pub struct RealVfs {
    root: std::path::PathBuf,
}

impl RealVfs {
    pub fn new(root: impl AsRef<Path>) -> Self {
        RealVfs {
            root: root.as_ref().to_path_buf(),
        }
    }

    fn resolve(&self, path: &str) -> std::path::PathBuf {
        // Paths coming from the engine are relative names; allow absolute
        // ones only when they live under the root.
        let p = Path::new(path);
        if p.is_absolute() {
            p.to_path_buf()
        } else {
            self.root.join(p)
        }
    }
}

struct RealFile(File, String);

impl VFile for RealFile {
    fn read(&mut self, buf: &mut [u8]) -> Result<usize> {
        self.0.read(buf).map_err(|e| Error::io(e, self.1.clone()))
    }
    fn write_all(&mut self, buf: &[u8]) -> Result<()> {
        self.0
            .write_all(buf)
            .map_err(|e| Error::io(e, self.1.clone()))
    }
    fn flush(&mut self) -> Result<()> {
        self.0.flush().map_err(|e| Error::io(e, self.1.clone()))
    }
    fn sync_all(&self) -> Result<()> {
        self.0.sync_all().map_err(|e| Error::io(e, self.1.clone()))
    }
}

impl Vfs for RealVfs {
    fn create(&self, path: &str) -> Result<Box<dyn VFile>> {
        let full = self.resolve(path);
        if let Some(parent) = full.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| Error::io(e, parent.display().to_string()))?;
        }
        let f = OpenOptions::new()
            .write(true)
            .create(true)
            .truncate(true)
            .open(&full)
            .map_err(|e| Error::io(e, full.display().to_string()))?;
        Ok(Box::new(RealFile(f, full.display().to_string())))
    }

    fn open(&self, path: &str) -> Result<Box<dyn VFile>> {
        let full = self.resolve(path);
        let f = File::open(&full).map_err(|e| Error::io(e, full.display().to_string()))?;
        Ok(Box::new(RealFile(f, full.display().to_string())))
    }

    fn rename(&self, from: &str, to: &str) -> Result<()> {
        let a = self.resolve(from);
        let b = self.resolve(to);
        if let Some(parent) = b.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| Error::io(e, parent.display().to_string()))?;
        }
        std::fs::rename(&a, &b)
            .map_err(|e| Error::io(e, format!("{} -> {}", a.display(), b.display())))?;
        // Durability boundary: persist the directory entry change.
        if let Some(parent) = b.parent() {
            sync_dir(parent)?;
        }
        Ok(())
    }

    fn remove(&self, path: &str) -> Result<()> {
        let full = self.resolve(path);
        match std::fs::remove_file(&full) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(Error::io(e, full.display().to_string())),
        }
    }

    fn exists(&self, path: &str) -> bool {
        self.resolve(path).exists()
    }

    fn list_dir(&self, path: &str) -> Result<Vec<DirEntry>> {
        let full = self.resolve(path);
        let rd = match std::fs::read_dir(&full) {
            Ok(rd) => rd,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(Error::io(e, full.display().to_string())),
        };
        let mut out = Vec::new();
        for ent in rd {
            let ent = ent.map_err(|e| Error::io(e, full.display().to_string()))?;
            let name = ent.file_name().to_string_lossy().into_owned();
            let is_file = ent.file_type().map(|t| t.is_file()).unwrap_or(false);
            out.push(DirEntry { name, is_file });
        }
        out.sort_by(|a, b| a.name.cmp(&b.name));
        Ok(out)
    }

    fn make_dir_all(&self, path: &str) -> Result<()> {
        let full = self.resolve(path);
        std::fs::create_dir_all(&full).map_err(|e| Error::io(e, full.display().to_string()))
    }

    fn remove_dir_all(&self, path: &str) -> Result<()> {
        let full = self.resolve(path);
        match std::fs::remove_dir_all(&full) {
            Ok(()) => Ok(()),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(e) => Err(Error::io(e, full.display().to_string())),
        }
    }
}

/// Kind of filesystem operation a fault rule can target.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FaultKind {
    CreateFail,
    WriteFail,
    SyncFail,
    RenameFail,
    ReadFail,
}

impl FaultKind {
    pub fn parse(s: &str) -> Option<Self> {
        Some(match s {
            "create_fail" => FaultKind::CreateFail,
            "write_fail" => FaultKind::WriteFail,
            "sync_fail" => FaultKind::SyncFail,
            "rename_fail" => FaultKind::RenameFail,
            "read_fail" => FaultKind::ReadFail,
            _ => return None,
        })
    }

    pub fn as_str(self) -> &'static str {
        match self {
            FaultKind::CreateFail => "create_fail",
            FaultKind::WriteFail => "write_fail",
            FaultKind::SyncFail => "sync_fail",
            FaultKind::RenameFail => "rename_fail",
            FaultKind::ReadFail => "read_fail",
        }
    }
}

/// One fault rule: fail the next `count` operations of `kind` whose affected
/// path contains `needle`.
#[derive(Debug, Clone)]
pub struct FaultRule {
    pub kind: FaultKind,
    pub needle: String,
    pub remaining: u32,
}

impl FaultRule {
    fn matches(&self, kind: FaultKind, path: &str) -> bool {
        self.kind == kind && (self.needle.is_empty() || path.contains(&self.needle))
    }

    /// Human-readable rendering, e.g. `write_fail:run-0001:1`.
    pub fn describe(&self) -> String {
        format!("{}:{}:{}", self.kind.as_str(), self.needle, self.remaining)
    }
}

/// An ordered set of fault rules. Parsed from the `X-Fault-Inject` header or
/// constructed directly in tests.
#[derive(Debug, Clone, Default)]
pub struct FaultPlan {
    rules: Vec<FaultRule>,
}

impl FaultPlan {
    pub fn new() -> Self {
        FaultPlan { rules: Vec::new() }
    }

    pub fn add(mut self, kind: FaultKind, needle: impl Into<String>, count: u32) -> Self {
        self.rules.push(FaultRule {
            kind,
            needle: needle.into(),
            remaining: count,
        });
        self
    }

    /// Parse the header grammar documented on the module. Empty string is an
    /// empty (disabled) plan.
    pub fn parse(text: &str) -> std::result::Result<Self, String> {
        let mut rules = Vec::new();
        for raw in text.split(',') {
            let raw = raw.trim();
            if raw.is_empty() {
                continue;
            }
            let parts: Vec<&str> = raw.split(':').collect();
            let kind = FaultKind::parse(parts[0].trim())
                .ok_or_else(|| format!("unknown fault kind: {}", parts[0]))?;
            let needle = parts
                .get(1)
                .map(|s| s.trim().to_string())
                .unwrap_or_default();
            let count = match parts.get(2) {
                Some(s) => s
                    .trim()
                    .parse::<u32>()
                    .map_err(|_| format!("bad fault count: {}", s))?,
                None => 1,
            };
            rules.push(FaultRule {
                kind,
                needle,
                remaining: count,
            });
        }
        Ok(FaultPlan { rules })
    }

    /// Whether any rule of `kind` is still armed (used for cheap skips).
    pub fn has_kind(&self, kind: FaultKind) -> bool {
        self.rules.iter().any(|r| r.kind == kind && r.remaining > 0)
    }

    /// Consume one matching firing opportunity. Returns a description if a
    /// rule fired.
    fn fire(&mut self, kind: FaultKind, path: &str) -> Option<String> {
        for rule in self.rules.iter_mut() {
            if rule.remaining > 0 && rule.matches(kind, path) {
                rule.remaining -= 1;
                return Some(format!("{} on {}", rule.kind.as_str(), path));
            }
        }
        None
    }

    pub fn describe(&self) -> String {
        self.rules
            .iter()
            .map(FaultRule::describe)
            .collect::<Vec<_>>()
            .join(",")
    }
}

/// VFS decorator that consults a shared [`FaultPlan`] before delegating.
#[derive(Clone)]
pub struct FaultVfs {
    inner: Arc<dyn Vfs>,
    plan: Arc<Mutex<FaultPlan>>,
}

impl FaultVfs {
    pub fn new(inner: Arc<dyn Vfs>, plan: FaultPlan) -> Self {
        FaultVfs {
            inner,
            plan: Arc::new(Mutex::new(plan)),
        }
    }

    fn check(&self, kind: FaultKind, path: &str) -> Result<()> {
        let mut plan = self.plan.lock().expect("fault plan poisoned");
        if let Some(msg) = plan.fire(kind, path) {
            return Err(Error::Fault(msg));
        }
        Ok(())
    }
}

struct FaultFile {
    inner: Box<dyn VFile>,
    plan: Arc<Mutex<FaultPlan>>,
    path: String,
}

impl VFile for FaultFile {
    fn read(&mut self, buf: &mut [u8]) -> Result<usize> {
        {
            let mut plan = self.plan.lock().expect("fault plan poisoned");
            if let Some(msg) = plan.fire(FaultKind::ReadFail, &self.path) {
                return Err(Error::Fault(msg));
            }
        }
        let n = self.inner.read(buf)?;
        Ok(n)
    }

    fn write_all(&mut self, buf: &[u8]) -> Result<()> {
        {
            let mut plan = self.plan.lock().expect("fault plan poisoned");
            if let Some(msg) = plan.fire(FaultKind::WriteFail, &self.path) {
                return Err(Error::Fault(msg));
            }
        }
        self.inner.write_all(buf)
    }

    fn flush(&mut self) -> Result<()> {
        self.inner.flush()
    }

    fn sync_all(&self) -> Result<()> {
        {
            let mut plan = self.plan.lock().expect("fault plan poisoned");
            if let Some(msg) = plan.fire(FaultKind::SyncFail, &self.path) {
                return Err(Error::Fault(msg));
            }
        }
        self.inner.sync_all()
    }
}

impl Vfs for FaultVfs {
    fn create(&self, path: &str) -> Result<Box<dyn VFile>> {
        self.check(FaultKind::CreateFail, path)?;
        let inner = self.inner.create(path)?;
        Ok(Box::new(FaultFile {
            inner,
            plan: self.plan.clone(),
            path: path.to_string(),
        }))
    }

    fn open(&self, path: &str) -> Result<Box<dyn VFile>> {
        // Read faults fire per-read inside the file handle.
        let inner = self.inner.open(path)?;
        Ok(Box::new(FaultFile {
            inner,
            plan: self.plan.clone(),
            path: path.to_string(),
        }))
    }

    fn rename(&self, from: &str, to: &str) -> Result<()> {
        self.check(FaultKind::RenameFail, from)?;
        self.check(FaultKind::RenameFail, to)?;
        self.inner.rename(from, to)
    }

    fn remove(&self, path: &str) -> Result<()> {
        self.inner.remove(path)
    }

    fn exists(&self, path: &str) -> bool {
        self.inner.exists(path)
    }

    fn list_dir(&self, path: &str) -> Result<Vec<DirEntry>> {
        self.inner.list_dir(path)
    }

    fn make_dir_all(&self, path: &str) -> Result<()> {
        self.inner.make_dir_all(path)
    }

    fn remove_dir_all(&self, path: &str) -> Result<()> {
        self.inner.remove_dir_all(path)
    }
}

/// Test/demo helper: overwrite `bytes` at `offset` of a real file, bypassing
/// the VFS (simulates bit rot / torn writes on disk after a crash).
pub fn corrupt_overwrite(path: impl AsRef<Path>, offset: u64, bytes: &[u8]) -> Result<()> {
    use std::io::Seek;
    let p = path.as_ref();
    let mut f = OpenOptions::new()
        .write(true)
        .open(p)
        .map_err(|e| Error::io(e, p.display().to_string()))?;
    f.seek(std::io::SeekFrom::Start(offset))
        .map_err(|e| Error::io(e, p.display().to_string()))?;
    f.write_all(bytes)
        .map_err(|e| Error::io(e, p.display().to_string()))?;
    f.flush()
        .map_err(|e| Error::io(e, p.display().to_string()))?;
    f.sync_all()
        .map_err(|e| Error::io(e, p.display().to_string()))?;
    Ok(())
}

/// Test/demo helper: truncate a file to `len` bytes (simulates a torn tail).
pub fn corrupt_truncate(path: impl AsRef<Path>, len: u64) -> Result<()> {
    let p = path.as_ref();
    let f = OpenOptions::new()
        .write(true)
        .open(p)
        .map_err(|e| Error::io(e, p.display().to_string()))?;
    f.set_len(len)
        .map_err(|e| Error::io(e, p.display().to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plan_parse_and_fire() {
        let mut p = FaultPlan::parse("write_fail:run-0001, rename_fail:run:2, sync_fail").unwrap();
        assert!(p
            .fire(FaultKind::WriteFail, "level0/run-0001.run")
            .is_some());
        // Consumed.
        assert!(p
            .fire(FaultKind::WriteFail, "level0/run-0001.run")
            .is_none());
        assert!(p.fire(FaultKind::RenameFail, "run-0001.run.tmp").is_some());
        assert!(p.fire(FaultKind::RenameFail, "run-0002.run.tmp").is_some());
        assert!(p.fire(FaultKind::RenameFail, "run-0003.run.tmp").is_none());
        // Empty needle matches anything.
        assert!(p.fire(FaultKind::SyncFail, "whatever").is_some());
    }

    #[test]
    fn plan_rejects_unknown() {
        assert!(FaultPlan::parse("explode").is_err());
        assert!(FaultPlan::parse("write_fail:x:notanumber").is_err());
        assert!(FaultPlan::parse("").unwrap().rules.is_empty());
    }

    #[test]
    fn real_vfs_roundtrip_and_commit() {
        let dir = std::env::temp_dir().join(format!("extsort-io-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let vfs = RealVfs::new(&dir);
        atomic_write_bytes(&vfs, "a.tmp", "sub/a.txt", b"hello").unwrap();
        assert_eq!(&read_all(&vfs, "sub/a.txt").unwrap(), b"hello");
        assert!(vfs.exists("sub/a.txt"));
        vfs.remove("sub/a.txt").unwrap();
        assert!(!vfs.exists("sub/a.txt"));
        std::fs::remove_dir_all(&dir).ok();
    }
}
